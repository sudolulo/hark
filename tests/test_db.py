import re
import sqlite3

import pytest

from hark import db


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def _schema_without_column(table: str, column: str) -> str:
    """db.SCHEMA with one column's definition line stripped — used to simulate
    a pre-migration database. Matches on the column name token, not exact
    whitespace, so it doesn't silently no-op if SCHEMA's alignment changes."""
    pattern = rf"\n\s*{re.escape(column)}\s+\S+,"
    new_schema, n = re.subn(pattern, "", db.SCHEMA, count=1)
    assert n == 1, f"{column!r} not found in SCHEMA — test fixture is stale"
    return new_schema


def test_connect_creates_tables(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"shows", "episodes", "topics", "topic_genres", "episode_topics",
            "ad_segments"} <= tables


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "test.db"
    db.connect(path).close()
    db.connect(path).close()


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO episodes (show_id, guid) VALUES (999, 'x')")


def test_episode_guid_unique_per_show(conn):
    conn.execute("INSERT INTO shows (query) VALUES ('a')")
    conn.execute("INSERT INTO shows (query) VALUES ('b')")
    conn.execute("INSERT INTO episodes (show_id, guid) VALUES (1, 'ep-1')")
    # same guid on another show is fine
    conn.execute("INSERT INTO episodes (show_id, guid) VALUES (2, 'ep-1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO episodes (show_id, guid) VALUES (1, 'ep-1')")


def test_migration_adds_extracted_at_to_old_db(tmp_path):
    """A 0.1.0-era database (no extracted_at) gains the column on connect."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(_schema_without_column("episodes", "extracted_at"))
    old.execute("INSERT INTO shows (query) VALUES ('a')")
    old.execute("INSERT INTO episodes (show_id, guid) VALUES (1, 'g')")
    old.commit()
    old.close()

    conn = db.connect(path)
    row = conn.execute("SELECT extracted_at FROM episodes").fetchone()
    assert row["extracted_at"] is None


@pytest.mark.parametrize("column", [
    "chapters_url", "chapters_scanned_at", "transcript_path", "llm_detected_at", "cut_path",
])
def test_migration_adds_ad_pipeline_columns_to_old_episodes(tmp_path, column):
    """A pre-merge database (no ad-stripping columns) gains them on connect —
    the ad_segments table itself is CREATE TABLE IF NOT EXISTS so it doesn't
    need a migration, only columns bolted onto the pre-existing episodes table do."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(_schema_without_column("episodes", column))
    old.execute("INSERT INTO shows (query) VALUES ('a')")
    old.execute("INSERT INTO episodes (show_id, guid) VALUES (1, 'g')")
    old.commit()
    old.close()

    conn = db.connect(path)
    row = conn.execute(f"SELECT {column} FROM episodes").fetchone()
    assert row[column] is None


def test_migration_adds_feed_token_to_old_shows(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(_schema_without_column("shows", "feed_token"))
    old.execute("INSERT INTO shows (query) VALUES ('a')")
    old.commit()
    old.close()

    conn = db.connect(path)
    row = conn.execute("SELECT feed_token FROM shows").fetchone()
    assert row["feed_token"] is not None  # backfilled, not just added-and-null


def test_backfill_feed_tokens_gives_every_show_a_unique_token(tmp_path):
    # backfill runs at connect() time, same as migrations — a show inserted on
    # an already-open connection doesn't get one until the next reconnect,
    # which matches real operation (every CLI command opens a fresh connection).
    path = tmp_path / "test.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query) VALUES ('a')")
    conn.execute("INSERT INTO shows (query) VALUES ('b')")
    conn.commit()
    conn.close()

    conn = db.connect(path)
    tokens = [row["feed_token"] for row in conn.execute("SELECT feed_token FROM shows")]
    assert all(tokens)
    assert len(set(tokens)) == 2


def test_backfill_feed_tokens_is_stable_across_reconnects(tmp_path):
    path = tmp_path / "test.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query) VALUES ('a')")
    conn.commit()
    conn.close()

    conn = db.connect(path)
    token = conn.execute("SELECT feed_token FROM shows").fetchone()[0]
    conn.close()

    conn2 = db.connect(path)
    assert conn2.execute("SELECT feed_token FROM shows").fetchone()[0] == token


def test_ad_segments_cascade_on_episode_delete(conn):
    conn.execute("INSERT INTO shows (query) VALUES ('a')")
    conn.execute("INSERT INTO episodes (show_id, guid) VALUES (1, 'ep-1')")
    conn.execute(
        "INSERT INTO ad_segments (episode_id, start_second, end_second, source)"
        " VALUES (1, 0, 30, 'chapter')"
    )
    conn.execute("DELETE FROM episodes WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) FROM ad_segments").fetchone()[0] == 0


def test_utcnow_format():
    value = db.utcnow()
    assert len(value) == 20 and value.endswith("Z") and value[10] == "T"
