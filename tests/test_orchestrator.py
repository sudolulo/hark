import sqlite3
import pytest
from hark import orchestrator, llm_budget


def _db(tmp_path):
    p = tmp_path / "t.db"
    sqlite3.connect(p).close()
    return str(p)


def test_cycle_runs_fast_stages_and_records_cadence(tmp_path):
    ran = []
    db = _db(tmp_path)
    out = orchestrator.run_cycle(db, now=1_000_000.0, data_dir=str(tmp_path),
                                 run=lambda argv: ran.append(argv) or 0, key_present=False)
    names = {a[0] for a in ran}
    assert "repeats" in names and "cut" in names and "fingerprint" in names  # fast stages ran
    assert {n for n, o in out if o == "ran"} >= {"repeats", "cut"}


def test_slow_stages_respect_cadence(tmp_path):
    db = _db(tmp_path)
    r1 = []
    orchestrator.run_cycle(db, now=1_000_000.0, data_dir=str(tmp_path),
                           run=lambda a: r1.append(a[0]) or 0, key_present=False)
    assert "ingest" in r1                                   # first pass: slow stage runs
    r2 = []
    orchestrator.run_cycle(db, now=1_000_000.0 + 60, data_dir=str(tmp_path),
                           run=lambda a: r2.append(a[0]) or 0, key_present=False)
    assert "ingest" not in r2                               # 60s later: not due (SLOW=1800)
    assert "repeats" in r2                                  # fast stage always due
    r3 = []
    orchestrator.run_cycle(db, now=1_000_000.0 + 2000, data_dir=str(tmp_path),
                           run=lambda a: r3.append(a[0]) or 0, key_present=False)
    assert "ingest" in r3                                   # >1800s: due again


def test_llm_stage_needs_a_key(tmp_path):
    db = _db(tmp_path)
    out = dict(orchestrator.run_cycle(db, now=1_000_000.0, data_dir=str(tmp_path),
                                      run=lambda a: 0, key_present=False))
    assert out["detect-ads"] == "skipped:no-key"


def test_llm_stage_needs_budget_even_with_a_key(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.delenv("HARK_LLM_DAILY_BUDGET", raising=False)  # no budget -> remaining()==0
    out = dict(orchestrator.run_cycle(db, now=1_000_000.0, data_dir=str(tmp_path),
                                      run=lambda a: 0, key_present=True))
    assert out["detect-ads"] == "skipped:budget"


def test_llm_stage_runs_with_key_and_budget(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setenv("HARK_LLM_DAILY_BUDGET", "5")
    ran = []
    orchestrator.run_cycle(db, now=1_000_000.0, data_dir=str(tmp_path),
                           run=lambda a: ran.append(a[0]) or 0, key_present=True)
    assert "detect-ads" in ran


def test_drop_files_are_loaded_and_archived(tmp_path):
    db = _db(tmp_path)
    drop = tmp_path / "pending-ad-detections.jsonl"
    drop.write_text('{"episode_id": 1, "ad_spans": []}\n')
    ran = []
    orchestrator.run_cycle(db, now=1_000_000.0, data_dir=str(tmp_path),
                           run=lambda a: ran.append(a) or 0, key_present=False)
    assert ["load-ad-detections", str(drop)] in ran
    assert not drop.exists()                                    # archived after a successful load
    assert list(tmp_path.glob("loaded-*-ad-detections.jsonl"))


# --- budget ---


def test_budget_off_without_a_cap(tmp_path, monkeypatch):
    monkeypatch.delenv("HARK_LLM_DAILY_BUDGET", raising=False)
    conn = sqlite3.connect(_db(tmp_path))
    assert llm_budget.daily_cap() == 0.0
    assert llm_budget.remaining(conn) == 0.0        # a key alone must not spend


def test_budget_records_and_depletes(tmp_path, monkeypatch):
    monkeypatch.setenv("HARK_LLM_DAILY_BUDGET", "1.00")
    conn = sqlite3.connect(_db(tmp_path))
    assert llm_budget.remaining(conn) == pytest.approx(1.0)
    llm_budget.record(conn, 0.30)
    llm_budget.record(conn, 0.30)
    assert llm_budget.spent_today(conn) == pytest.approx(0.60)
    assert llm_budget.remaining(conn) == pytest.approx(0.40)
    llm_budget.record(conn, 0.50)
    assert llm_budget.remaining(conn) == 0.0        # clamped, never negative


# --- heartbeat + log rotation ---


def test_run_cycle_streams_a_per_stage_heartbeat(tmp_path):
    db = _db(tmp_path)
    beats = []
    orchestrator.run_cycle(db, now=1_000_000.0, data_dir=str(tmp_path),
                           run=lambda a: 0, key_present=False, log=beats.append)
    assert "→ repeats" in beats and "ran repeats" in beats      # start (→) then done
    assert beats.index("→ repeats") < beats.index("ran repeats")
    assert not any("detect-ads" in b for b in beats)                # a skipped stage is silent


def test_run_cycle_heartbeat_flags_a_nonzero_exit(tmp_path):
    db = _db(tmp_path)
    beats = []
    orchestrator.run_cycle(db, now=1_000_000.0, data_dir=str(tmp_path),
                           run=lambda a: 3, key_present=False, log=beats.append)
    assert "ran repeats (exit 3)" in beats                           # a failed stage is visible


def test_rotate_log_copytruncates_when_over_cap(tmp_path):
    log = tmp_path / "transcribe.log"
    log.write_text("x" * 100)
    assert orchestrator.rotate_log(str(log), max_bytes=50) is True
    assert log.read_text() == ""                                     # original truncated IN PLACE
    assert (tmp_path / "transcribe.log.1").read_text() == "x" * 100  # exactly one backup kept


def test_rotate_log_leaves_a_small_log_alone(tmp_path):
    log = tmp_path / "transcribe.log"
    log.write_text("x" * 40)
    assert orchestrator.rotate_log(str(log), max_bytes=50) is False
    assert log.read_text() == "x" * 40
    assert not (tmp_path / "transcribe.log.1").exists()


def test_rotate_log_missing_or_disabled_is_a_noop(tmp_path):
    assert orchestrator.rotate_log(str(tmp_path / "nope.log"), max_bytes=1) is False
    present = tmp_path / "transcribe.log"
    present.write_text("x" * 100)
    assert orchestrator.rotate_log(str(present), max_bytes=0) is False   # <=0 disables rotation
    assert present.read_text() == "x" * 100


def test_default_run_spawns_a_real_hark_process(tmp_path):
    """The orchestrator shells out via `python -m hark`; prove that entry point exists and a
    harmless stage returns 0 (guards against a missing __main__.py breaking every stage)."""
    from hark import db as _db
    p = str(tmp_path / "t.db"); _db.connect(p).close()
    assert orchestrator._default_run(p, ["stats"]) == 0
