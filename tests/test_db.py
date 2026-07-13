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
    whitespace, so it doesn't silently no-op if SCHEMA's alignment changes.
    The definition itself may be single-token (`TEXT,`) or multi-word
    (`INTEGER NOT NULL DEFAULT 1,`) — matches up to the first comma either way.

    Scoped to the named table's own `CREATE TABLE ... (...)` block, not the
    whole SCHEMA string — several tables now have their own same-named
    `user_id` column, so a whole-file match could silently strip the wrong
    table's column (and, worse, leave a UNIQUE constraint elsewhere in that
    same block referencing the now-missing column, breaking CREATE TABLE
    entirely rather than just failing this helper's own assertion)."""
    table_pattern = rf"CREATE TABLE IF NOT EXISTS {re.escape(table)} \([^;]*?\);"
    match = re.search(table_pattern, db.SCHEMA)
    assert match, f"{table!r} not found in SCHEMA — test fixture is stale"
    block = match.group(0)
    column_pattern = rf"\n\s*{re.escape(column)}\s+[^\n,]+,"
    new_block, n = re.subn(column_pattern, "", block, count=1)
    assert n == 1, f"{column!r} not found in {table!r}'s block — test fixture is stale"
    return db.SCHEMA[:match.start()] + new_block + db.SCHEMA[match.end():]


def test_connect_creates_tables(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"shows", "episodes", "topics", "topic_genres", "episode_topics",
            "ad_segments", "show_ratings"} <= tables


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
    """A pre-ad-pipeline database gains these columns on connect — the
    ad_segments table itself is CREATE TABLE IF NOT EXISTS so it doesn't need
    a migration, only columns bolted onto the pre-existing episodes table do."""
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


def test_migration_backfills_topic_index_disabled_for_bare_row_shows(tmp_path):
    """A pre-0.10.0 database's existing shows get retroactively split: shows
    added via resolve.add_show_by_feed_url() (query == feed_url, no search
    term to record) land disabled; hand-curated hark-resolve shows (query is
    a human-typed name) keep the schema's DEFAULT 1."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(_schema_without_column("shows", "topic_index_enabled"))
    old.execute("INSERT INTO shows (query, feed_url) VALUES ('Casefile True Crime', 'http://a')")
    old.execute("INSERT INTO shows (query, feed_url) VALUES ('http://b', 'http://b')")
    old.commit()
    old.close()

    conn = db.connect(path)
    curated = conn.execute(
        "SELECT topic_index_enabled FROM shows WHERE query = 'Casefile True Crime'"
    ).fetchone()
    bare_row = conn.execute(
        "SELECT topic_index_enabled FROM shows WHERE query = 'http://b'"
    ).fetchone()
    assert curated["topic_index_enabled"] == 1
    assert bare_row["topic_index_enabled"] == 0


def test_topic_index_backfill_does_not_repeat_on_later_connect(tmp_path):
    # The backfill must only run once, alongside the ALTER TABLE — otherwise
    # an owner's manual re-enable (via the show page) of a bare-row show
    # would get silently reverted on the app's next restart.
    path = tmp_path / "test.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query, feed_url) VALUES ('http://a', 'http://a')")
    conn.commit()
    conn.execute("UPDATE shows SET topic_index_enabled = 1 WHERE query = 'http://a'")
    conn.commit()
    conn.close()

    reconnected = db.connect(path)
    row = reconnected.execute(
        "SELECT topic_index_enabled FROM shows WHERE query = 'http://a'"
    ).fetchone()
    assert row["topic_index_enabled"] == 1  # not stomped back to 0


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


def test_ad_segments_cascade_on_episode_delete(conn):
    conn.execute("INSERT INTO shows (query) VALUES ('a')")
    conn.execute("INSERT INTO episodes (show_id, guid) VALUES (1, 'ep-1')")
    conn.execute(
        "INSERT INTO ad_segments (episode_id, start_second, end_second, source)"
        " VALUES (1, 0, 30, 'chapter')"
    )
    conn.execute("DELETE FROM episodes WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) FROM ad_segments").fetchone()[0] == 0


def test_migration_adds_user_id_to_old_subscription_changes(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(_schema_without_column("subscription_changes", "user_id"))
    old.execute(
        "INSERT INTO subscription_changes (feed_url, action, occurred_at) VALUES (?, 'add', 1)",
        ("http://a",),
    )
    old.commit()
    old.close()

    conn = db.connect(path)
    row = conn.execute("SELECT user_id FROM subscription_changes").fetchone()
    assert row["user_id"] == 1  # pre-existing rows attributed to the bootstrap account


def _old_listen_actions_schema() -> str:
    """A pre-0.14.0 listen_actions: no user_id, UNIQUE without it."""
    return """
    CREATE TABLE listen_actions (
        id           INTEGER PRIMARY KEY,
        podcast_url  TEXT NOT NULL,
        episode_url  TEXT NOT NULL,
        episode_guid TEXT,
        action       TEXT NOT NULL,
        started      INTEGER,
        position     INTEGER,
        total        INTEGER,
        occurred_at  TEXT,
        created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        UNIQUE (podcast_url, episode_url, action, occurred_at)
    );
    """


def test_migration_rebuilds_listen_actions_with_user_id(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(_old_listen_actions_schema())
    old.execute(
        "INSERT INTO listen_actions (podcast_url, episode_url, action, started, position, total,"
        " occurred_at) VALUES ('http://a', 'http://a/1.mp3', 'play', 5, 10, 100,"
        " '2026-01-01T00:00:00')"
    )
    old.commit()
    old.close()

    conn = db.connect(path)
    row = conn.execute("SELECT * FROM listen_actions").fetchone()
    assert row["user_id"] == 1  # pre-existing row attributed to the bootstrap account
    assert row["podcast_url"] == "http://a"
    assert row["started"] == 5 and row["position"] == 10 and row["total"] == 100


def test_migration_listen_actions_new_unique_constraint_scoped_by_user(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(_old_listen_actions_schema())
    old.close()

    conn = db.connect(path)
    conn.execute(
        "INSERT INTO listen_actions (user_id, podcast_url, episode_url, action, occurred_at)"
        " VALUES (1, 'http://a', 'http://a/1.mp3', 'play', 't')"
    )
    # Same everything except user_id — must NOT collide with the row above,
    # which a pre-migration UNIQUE constraint (no user_id) would have.
    conn.execute(
        "INSERT INTO listen_actions (user_id, podcast_url, episode_url, action, occurred_at)"
        " VALUES (2, 'http://a', 'http://a/1.mp3', 'play', 't')"
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM listen_actions").fetchone()[0] == 2


def test_listen_actions_migration_does_not_repeat_on_later_connect(tmp_path):
    path = tmp_path / "test.db"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO listen_actions (user_id, podcast_url, episode_url, action, occurred_at)"
        " VALUES (2, 'http://a', 'http://a/1.mp3', 'play', 't')"
    )
    conn.commit()
    conn.close()

    reconnected = db.connect(path)
    row = reconnected.execute("SELECT user_id FROM listen_actions").fetchone()
    assert row["user_id"] == 2  # not stomped back to the DEFAULT 1


def test_user_shows_backfilled_for_shows_that_predate_it(tmp_path):
    """A pre-0.14.0 database's existing shows all become visible to the
    bootstrap account (user 1) — upgrading shouldn't blank out the one real
    account's dashboard. Simulated with the full current schema minus
    user_shows (and its index, which would otherwise dangle) — using the
    real schema for everything else, rather than hand-rolling an "old"
    version, is what actually exercises connect()'s pre-CREATE existence
    check on user_shows specifically."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)
    old.execute("DROP INDEX idx_user_shows_user")
    old.execute("DROP TABLE user_shows")
    old.execute("INSERT INTO shows (query) VALUES ('a')")
    old.execute("INSERT INTO shows (query) VALUES ('b')")
    old.commit()
    old.close()

    conn = db.connect(path)
    rows = conn.execute("SELECT show_id FROM user_shows WHERE user_id = 1 ORDER BY show_id").fetchall()
    assert [r["show_id"] for r in rows] == [1, 2]


def test_user_shows_backfill_does_not_repeat_on_later_connect(tmp_path):
    # A user unsubscribing (DELETE FROM user_shows) must not get re-added by
    # the backfill running again on the app's next restart.
    path = tmp_path / "test.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query) VALUES ('a')")
    conn.commit()
    conn.close()

    conn = db.connect(path)  # user_shows backfilled here (table was new)
    conn.execute("DELETE FROM user_shows WHERE user_id = 1 AND show_id = 1")
    conn.commit()
    conn.close()

    reconnected = db.connect(path)
    assert reconnected.execute(
        "SELECT COUNT(*) FROM user_shows WHERE user_id = 1 AND show_id = 1"
    ).fetchone()[0] == 0


def test_user_shows_cascades_on_show_delete(conn):
    conn.execute("INSERT INTO shows (query) VALUES ('a')")
    conn.execute("INSERT INTO user_shows (user_id, show_id) VALUES (1, 1)")
    conn.execute("DELETE FROM shows WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) FROM user_shows").fetchone()[0] == 0


def test_show_ratings_cascades_on_show_delete(conn):
    conn.execute("INSERT INTO shows (query) VALUES ('a')")
    conn.execute(
        "INSERT INTO show_ratings (show_id, source, fetched_at) VALUES (1, 'podchaser', '2026-01-01T00:00:00Z')"
    )
    conn.execute("DELETE FROM shows WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) FROM show_ratings").fetchone()[0] == 0


def test_utcnow_format():
    value = db.utcnow()
    assert len(value) == 20 and value.endswith("Z") and value[10] == "T"
