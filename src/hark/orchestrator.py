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

from . import alert, llm_budget

FAST = 0.0
SLOW = 1800.0  # 30 min — the "occasional" cadence for ingest/canon/chapters/dai/fingerprint-match
ALERT_COOLDOWN = 3600.0  # at most one ntfy alert per key (cycle / a given stage) per hour

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
    # per-platform 3 (was 1): more depth per cycle, and select_sample now skips proven-non-DAI
    # platforms so that budget lands on platforms that actually do dynamic insertion (5b).
    Stage("dai-probe", ["dai-probe", "--per-platform", "3"], SLOW),
    Stage("fp-index", ["fingerprint", "--index", "--limit", "30"], FAST),
    # STREAMING index (SLOW, small limit): fingerprint un-downloaded episodes by fetch-and-discard
    # so coverage reaches the whole corpus, not just the ~1.3k on disk. Bounded because each is a
    # full audio fetch — bandwidth, not storage. Local-audio index above stays FAST/cheap.
    Stage("fp-stream-index", ["fingerprint", "--index", "--stream", "--limit", "20"], SLOW),
    Stage("transcribe-cross", ["transcribe", "--cross-show-only", "--limit", "5"], FAST),
    Stage("transcribe", ["transcribe", "--limit", "20"], FAST),
    Stage("fp-match", ["fingerprint"], SLOW),
    # Free visibility into the library-bootstrap gap: logs how many ad campaigns still await a
    # ground-truth read. No key, no spend — just surfaces the number so it never goes unseen.
    Stage("ad-seeds", ["seeds", "--count"], SLOW),
    # Free drift signal for the inference tiers (span counts, median duration, suspect-short %).
    Stage("verify-inference", ["verify-inference"], SLOW),
    Stage("repeats", ["repeats"], FAST),
    Stage("detect-ads", ["detect-ads", "--limit", "5"], SLOW, needs_key=True, budget=llm_budget.ADS),
    # compare (treatment comparison across shows) — needs transcripts, so it runs after transcribe;
    # draws from the COMPARISONS pool, same as extract. Dormant until that budget is funded.
    Stage("compare", ["compare", "--limit", "3"], SLOW, needs_key=True, budget=llm_budget.COMPARISONS),
    Stage("cut", ["cut"], FAST),
]
# Deliberately NOT a stage: `discover-ads` (the `recur` tier). Its spans are neither cut
# (not in adscrub's CUT_SOURCES) nor a fingerprint-library seed (not in FP_LIBRARY_SOURCES), and
# the campaign machinery the pipeline DOES use (`ad-seeds` / `detect-ads` via find_campaigns)
# recomputes recurrence live from the fingerprint cache rather than from persisted `recur` rows.
# So wiring it would only write DB rows nothing reads. It stays a manual discovery tool.

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
    stage       TEXT PRIMARY KEY,
    last_run    REAL,          -- last time it actually RAN (0.0 = never); drives cadence
    last_status TEXT,          -- last outcome: ran | skipped:cadence | skipped:no-key | skipped:budget
    last_seen   REAL,          -- last cycle it was CONSIDERED (ran or skipped)
    last_exit   INTEGER        -- exit code of the last run (NULL until it has run once)
);
"""


def _ensure_pipeline_schema(conn: sqlite3.Connection) -> None:
    # The 0.26 table was (stage, last_run NOT NULL). Add the status columns in place so the UI can
    # show what each stage last did, not just when it last ran. last_run stays NOT NULL on the old
    # table, so a never-run stage is written as 0.0 (a sentinel that also reads as "past due").
    conn.executescript(_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_runs)")}
    for col, decl in (("last_status", "TEXT"), ("last_seen", "REAL"), ("last_exit", "INTEGER")):
        if col not in cols:
            conn.execute(f"ALTER TABLE pipeline_runs ADD COLUMN {col} {decl}")
    conn.commit()


def _record_stage(conn: sqlite3.Connection, stage: str, *, seen_at: float, status: str,
                  ran_at: float | None = None, exit_code: int | None = None) -> None:
    """Persist a stage's outcome this cycle. INSERT OR IGNORE first so a brand-new stage gets a
    row with last_run=0.0 (satisfies the old table's NOT NULL and reads as 'never ran'); then
    update — last_run/last_exit only when it actually ran, so a skip never clobbers the real
    last-run time cadence depends on."""
    conn.execute("INSERT OR IGNORE INTO pipeline_runs (stage, last_run) VALUES (?, 0.0)", (stage,))
    if ran_at is not None:
        conn.execute(
            "UPDATE pipeline_runs SET last_seen=?, last_status=?, last_run=?, last_exit=? "
            "WHERE stage=?", (seen_at, status, ran_at, exit_code, stage))
    else:
        conn.execute("UPDATE pipeline_runs SET last_seen=?, last_status=? WHERE stage=?",
                     (seen_at, status, stage))


def stage_meta() -> list[dict]:
    """Human-facing metadata for each stage — kept next to STAGES so the /pipeline dashboard's
    labels track the definition. `order` is the run order within a cycle."""
    out = []
    for i, st in enumerate(STAGES):
        if not st.needs_key and st.budget is None:
            gate = "free"
        elif st.budget == llm_budget.ADS:
            gate = "key + ads budget"
        elif st.budget == llm_budget.COMPARISONS:
            gate = "key + comparisons budget"
        else:
            gate = "key"
        out.append({
            "name": st.name,
            "order": i,
            "cadence": "every cycle" if st.every <= 0 else f"~{int(st.every // 60)} min",
            "gate": gate,
        })
    return out


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
    on_stage_error: Callable[[str, int], None] | None = None,
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
    _ensure_pipeline_schema(conn)
    last = {r[0]: (r[1] or 0.0) for r in conn.execute("SELECT stage, last_run FROM pipeline_runs")}
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
            status = "skipped:cadence"
        elif st.needs_key and not key_present:
            status = "skipped:no-key"
        elif st.budget is not None and llm_budget.remaining(conn, st.budget) <= 0:
            status = "skipped:budget"
        else:
            status = "ran"

        if status != "ran":
            _record_stage(conn, st.name, seen_at=now, status=status)
            conn.commit()
            outcomes.append((st.name, status))
            continue

        log(f"→ {st.name}")                        # start heartbeat, before the slow work
        rc = run(st.argv)
        _record_stage(conn, st.name, seen_at=now, status="ran", ran_at=now, exit_code=rc)
        conn.commit()
        outcomes.append((st.name, "ran"))
        log(f"ran {st.name}" + ("" if rc == 0 else f" (exit {rc})"))
        # A non-zero exit here now genuinely means a real error: empty queues and quarantined/
        # held episodes all exit 0 (see cli.py). So this is worth paging on.
        if rc != 0 and on_stage_error is not None:
            on_stage_error(st.name, rc)
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
         f"comparisons_budget=${llm_budget.daily_cap(llm_budget.COMPARISONS):.2f}/day, "
         f"alerts={'on' if alert.enabled() else 'off'}")

    last_alert: dict[str, float] = {}

    def _maybe_alert(key: str, title: str, message: str) -> None:
        # Dedup so a persistently-broken stage (or a wedged loop) pages at most once per hour,
        # not every 60s cycle. Only stamp on a successful send, so a transient ntfy blip doesn't
        # swallow a real alert.
        if time.time() - last_alert.get(key, 0.0) < ALERT_COOLDOWN:
            return
        if alert.notify(title, message):
            last_alert[key] = time.time()
            _log(f"alerted: {title}")

    def _on_stage_error(name: str, rc: int) -> None:
        _maybe_alert(f"stage:{name}", f"hark pipeline: {name} failed", f"stage '{name}' exited {rc}")

    while True:
        # Rotate BETWEEN cycles, while no stage subprocess is writing to the log.
        if rotate_log(log_path, log_max):
            _log(f"log rotated (> {log_max} bytes); previous kept at {log_path}.1")
        try:
            # run_cycle streams its own per-stage heartbeat via this hook.
            run_cycle(db_path, data_dir=data_dir, log=_log, on_stage_error=_on_stage_error)
        except Exception as exc:  # noqa: BLE001 — a bad cycle must not kill the loop
            _log(f"cycle error: {exc}")
            _maybe_alert("cycle", "hark pipeline: cycle error", str(exc))
        time.sleep(interval)
