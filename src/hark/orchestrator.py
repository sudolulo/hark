"""The ad/topic pipeline as a tested Python loop, replacing the 1,755-char shell string that
used to live in the container's compose command.

Why this exists as code and not shell:
  - the shell string was unversioned (lived in the TrueNAS app config), untestable, and changed
    by string surgery on production;
  - it was serial-by-accident, which is actually the right call for one SQLite writer — so this
    keeps the single-process, one-stage-at-a-time shape, but makes the ORCHESTRATION (order,
    cadence, gating) a first-class, tested thing.

Each stage declares its cadence (`every` seconds; 0 = every cycle) and its gates. Free stages
always run. LLM stages run only when a key is present AND the daily budget has room — that is how
"auto-enabled" stays safe: a key alone doesn't spend, you also set HARK_LLM_DAILY_BUDGET, and
`detect-ads` (ads-first, campaign-bounded) gets first claim on it.

Stages shell out to the same `hark` CLI, so each stage stays the command that is already tested
on its own; a crash in one is caught and logged, never kills the loop.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import llm_budget

FAST = 0.0
SLOW = 1800.0  # 30 min — the "occasional" cadence for ingest/canon/chapters/dai/fingerprint-match

# The container runs `hark pipeline >> /app/data/transcribe.log`, so the log grows unbounded
# unless the pipeline trims its own tail. Defaults picked for a homelab pool that runs near-full;
# override via env. LOG_MAX_BYTES <= 0 disables rotation entirely.
DEFAULT_LOG_PATH = "/app/data/transcribe.log"
DEFAULT_LOG_MAX_BYTES = 25 * 1024 * 1024  # 25 MB, then one .1 backup is kept


@dataclass(frozen=True)
class Stage:
    name: str
    argv: list[str]          # the `hark ...` subcommand + flags
    every: float = FAST
    needs_key: bool = False   # requires ANTHROPIC_API_KEY
    budget: str | None = None  # None = free; else the llm_budget category it draws from


# Order matters: index before match, transcribe before repeats/detect, detect before cut so new
# llm spans are cut the same cycle. Cadence then decides which of these actually fire per pass.
STAGES: list[Stage] = [
    Stage("sync-subscriptions", ["sync-subscriptions"], SLOW),
    Stage("sync-history", ["sync-history"], SLOW),
    Stage("ingest", ["ingest"], SLOW),
    # extract (topic index / comparison LLM) before canon, so topics it mints get canonicalised
    # the same cycle. Gated on the COMPARISONS budget — its own pool, independent of ads.
    Stage("extract", ["extract", "--limit", "20"], SLOW, needs_key=True, budget=llm_budget.COMPARISONS),
    Stage("canon", ["canon", "--limit", "50"], SLOW),
    Stage("chapters", ["chapters"], SLOW),
    Stage("dai-probe", ["dai-probe", "--per-platform", "1"], SLOW),
    Stage("fp-index", ["fingerprint", "--index", "--limit", "30"], FAST),
    Stage("transcribe-cross", ["transcribe", "--cross-show-only", "--limit", "5"], FAST),
    Stage("transcribe", ["transcribe", "--limit", "20"], FAST),
    Stage("fp-match", ["fingerprint"], SLOW),
    Stage("repeats", ["repeats"], FAST),
    Stage("detect-ads", ["detect-ads", "--limit", "5"], SLOW, needs_key=True, budget=llm_budget.ADS),
    Stage("cut", ["cut"], FAST),
]

# Session-as-extractor drop files: the monthly-Claude path. Loaded every cycle if present, then
# archived. Free (no key), and unchanged from the old loop — this is what keeps ad/topic work
# flowing without an API key.
DROP_FILES = [
    ("pending-comparisons.jsonl", ["load-comparisons"]),
    ("pending-extractions.jsonl", ["load"]),
    ("pending-ad-detections.jsonl", ["load-ad-detections"]),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    stage    TEXT PRIMARY KEY,
    last_run REAL NOT NULL
);
"""


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}] pipeline: {msg}", flush=True)


def _default_run(db_path: str, argv: list[str]) -> int:
    """Run one `hark` subcommand as its own process, so a crash is isolated."""
    try:
        return subprocess.run([sys.executable, "-m", "hark", "--db", db_path, *argv]).returncode
    except Exception as exc:  # noqa: BLE001
        _log(f"stage {' '.join(argv)} raised {exc}")
        return 1


def rotate_log(path: str, max_bytes: int) -> bool:
    """copytruncate rotation: if `path` is over `max_bytes`, copy it to `path.1` (one backup) and
    truncate the original IN PLACE. In-place truncation is required, not a rename: the container's
    shell holds the log open with O_APPEND (`>> transcribe.log`), so after an ftruncate its next
    write simply resumes at offset 0 — whereas a rename would leave the shell writing forever to
    the renamed-away inode and never touch the fresh file. Call it BETWEEN cycles, when no stage
    subprocess is writing, so there is no concurrent writer to race. Returns True if it rotated."""
    try:
        if max_bytes <= 0 or os.path.getsize(path) < max_bytes:
            return False
    except OSError:
        return False  # missing file (e.g. running without the `>>` redirect) — nothing to rotate
    try:
        with open(path, "rb") as src, open(path + ".1", "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.truncate(path, 0)
        return True
    except OSError:
        return False


def run_cycle(
    db_path: str,
    *,
    now: float | None = None,
    data_dir: str | None = None,
    run: Callable[[list[str]], int] | None = None,
    key_present: bool | None = None,
    log: Callable[[str], None] | None = None,
) -> list[tuple[str, str]]:
    """One pass. Returns (stage, "ran"|"skipped:<reason>") for each stage considered.

    `log`, if given, is called with a heartbeat the moment each stage STARTS (`→ <stage>`) and
    again when it finishes (`ran <stage>`) — so a long stage (whisper can take many minutes) is
    observable in real time instead of the whole cycle's outcomes landing in one burst at the end.
    The return value is unchanged, so `--once` and the tests don't need it."""
    now = time.time() if now is None else now
    data_dir = data_dir if data_dir is not None else os.environ.get("HARK_DATA_DIR", "/app/data")
    run = run or (lambda argv: _default_run(db_path, argv))
    log = log or (lambda _msg: None)
    key_present = (bool(os.environ.get("ANTHROPIC_API_KEY"))
                   if key_present is None else key_present)

    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    last = {r[0]: r[1] for r in conn.execute("SELECT stage, last_run FROM pipeline_runs")}
    outcomes: list[tuple[str, str]] = []

    # drop files first: cheap, and a fresh detection/extraction should be visible to this cycle
    for fname, argv in DROP_FILES:
        path = os.path.join(data_dir, fname)
        if os.path.exists(path):
            if run(argv + [path]) == 0:
                stamp = datetime.now(timezone.utc).strftime("%s")
                loaded = os.path.join(data_dir, fname.replace("pending-", f"loaded-{stamp}-"))
                try:
                    os.replace(path, loaded)
                except OSError:
                    pass
            outcomes.append((fname, "ran"))
            log(f"ran {fname}")

    for st in STAGES:
        if now - last.get(st.name, 0.0) < st.every:
            outcomes.append((st.name, "skipped:cadence"))
            continue
        if st.needs_key and not key_present:
            outcomes.append((st.name, "skipped:no-key"))
            continue
        if st.budget is not None and llm_budget.remaining(conn, st.budget) <= 0:
            outcomes.append((st.name, "skipped:budget"))
            continue
        log(f"→ {st.name}")                        # start heartbeat, before the slow work
        rc = run(st.argv)
        conn.execute(
            "INSERT INTO pipeline_runs (stage, last_run) VALUES (?, ?) "
            "ON CONFLICT(stage) DO UPDATE SET last_run = excluded.last_run",
            (st.name, now),
        )
        conn.commit()
        outcomes.append((st.name, "ran"))
        log(f"ran {st.name}" + ("" if rc == 0 else f" (exit {rc})"))
    conn.close()
    return outcomes


def run_loop(db_path: str, interval: float = 60.0, data_dir: str | None = None) -> None:
    log_path = os.environ.get("HARK_LOG_PATH", DEFAULT_LOG_PATH)
    try:
        log_max = int(os.environ.get("HARK_LOG_MAX_BYTES", str(DEFAULT_LOG_MAX_BYTES)))
    except ValueError:
        log_max = DEFAULT_LOG_MAX_BYTES
    _log(f"starting; interval={interval:.0f}s, key={'yes' if os.environ.get('ANTHROPIC_API_KEY') else 'no'}, "
         f"ads_budget=${llm_budget.daily_cap(llm_budget.ADS):.2f}/day, "
         f"comparisons_budget=${llm_budget.daily_cap(llm_budget.COMPARISONS):.2f}/day")
    while True:
        # Rotate BETWEEN cycles, while no stage subprocess is writing to the log.
        if rotate_log(log_path, log_max):
            _log(f"log rotated (> {log_max} bytes); previous kept at {log_path}.1")
        try:
            # run_cycle streams its own per-stage heartbeat via this hook.
            run_cycle(db_path, data_dir=data_dir, log=_log)
        except Exception as exc:  # noqa: BLE001 — a bad cycle must not kill the loop
            _log(f"cycle error: {exc}")
        time.sleep(interval)
