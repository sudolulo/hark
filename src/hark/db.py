"""SQLite storage: schema and connection helper."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# topics / topic_genres / episode_topics are created now but only populated by
# M1 extraction; wikidata_id, confidence, and source stay NULL until then.
#
# feed_token / chapters_* / transcript_path / llm_detected_at / cut_path / ad_segments
# are hark's own ad-stripping tracking — hark uses adscrub as a library (see
# pyproject.toml [tool.uv.sources]) for the reusable pipeline logic, but the state of
# *which of hark's own episodes* have been scanned/transcribed/detected/cut lives here,
# in hark's own schema, since that's tied to hark's own shows/episodes rows, not
# adscrub's separate database. feed_token gates the unauthenticated /feed and /audio
# routes (podcast apps can't do the dashboard's cookie login); every show gets one via
# _backfill_feed_tokens, not just ones added after this feature. ad_stripping_enabled
# gates whether the chapters/transcribe/detect-ads/cut pipeline touches a show at all —
# defaults ON (matches the pipeline's original unconditional behavior for every show
# that existed before this column did); the show page lets you switch specific shows
# off to save compute (transcription especially is real cost per episode).
#
# topic_index_enabled gates whether a show's episodes are eligible for topic
# extraction (pipeline.pending_episodes()) at all — separate from
# ad_stripping_enabled because they answer different questions: ad-stripping
# is meant to cover *every* subscription (CLAUDE.md), but topic extraction is
# only useful for subject-per-episode genre shows in the first place (a news
# roundtable or personal-finance show has no "real-world case" to extract,
# and running extraction on one anyway just burns session-as-X time on
# episodes that will always return an empty topic list). Defaults ON for the
# schema/hand-curated hark-resolve path, but resolve.add_show_by_feed_url()
# (gpodder sync, OPML import, discover --add — anything not individually
# reviewed) explicitly inserts it OFF pending a look at the show page. A
# 2026-07-12 gpodder sync added 67 such shows in one shot, most of which
# aren't genre-relevant at all — see _backfill_topic_index_enabled below for
# how existing rows got corrected retroactively.
SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS shows (
    id              INTEGER PRIMARY KEY,
    query           TEXT NOT NULL UNIQUE,
    title           TEXT,
    feed_url        TEXT UNIQUE,
    itunes_id       INTEGER,
    author          TEXT,
    description     TEXT,
    image_url       TEXT,
    feed_token      TEXT,
    ad_stripping_enabled INTEGER NOT NULL DEFAULT 1,
    topic_index_enabled  INTEGER NOT NULL DEFAULT 1,
    last_fetched_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS episodes (
    id                  INTEGER PRIMARY KEY,
    show_id             INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    guid                TEXT NOT NULL,
    title               TEXT,
    description         TEXT,
    pubdate             TEXT,
    duration_seconds    INTEGER,
    audio_url           TEXT,
    extracted_at        TEXT,
    chapters_url        TEXT,
    chapters_scanned_at TEXT,
    transcript_path     TEXT,
    llm_detected_at     TEXT,
    cut_path            TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at          TEXT,
    UNIQUE (show_id, guid)
);

CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY,
    label       TEXT NOT NULL UNIQUE,
    wikidata_id TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS topic_genres (
    topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    genre    TEXT NOT NULL,
    PRIMARY KEY (topic_id, genre)
);

CREATE TABLE IF NOT EXISTS episode_topics (
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    topic_id   INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    confidence REAL,
    source     TEXT,
    PRIMARY KEY (episode_id, topic_id)
);

-- Ad spans for an episode, however they were found. Multiple sources can
-- coexist (a chapter-sourced span later confirmed by transcript classification)
-- — dedup/precedence is a cut-time concern (overlap-merge), not a schema one.
CREATE TABLE IF NOT EXISTS ad_segments (
    id           INTEGER PRIMARY KEY,
    episode_id   INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    start_second REAL NOT NULL,
    end_second   REAL NOT NULL,
    source       TEXT NOT NULL,
    confidence   REAL,
    reason       TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_episodes_show_pubdate ON episodes (show_id, pubdate);
CREATE INDEX IF NOT EXISTS idx_ad_segments_episode ON ad_segments (episode_id);
-- episode_topics' PK is (episode_id, topic_id) — episode_id-leading, so it
-- doesn't help topic_id-first lookups. web.py's related_shows/related_topics
-- (topic co-occurrence) and view_topic's episode listing all filter/join on
-- topic_id on every page view; without this they're full table scans.
CREATE INDEX IF NOT EXISTS idx_episode_topics_topic ON episode_topics (topic_id);

-- M3: raw AntennaPod listen history. Populated two ways: read from
-- Nextcloud's GPodder Sync app (nextcloud.py, hark as *client*) or written
-- directly by AntennaPod if it's pointed at hark itself (gpodder_server.py,
-- hark as *server* — see that module's docstring for why no app fork is
-- needed for this). Stored as-is, keyed by feed/episode URL rather than
-- hark's own episode_id, since a listen can arrive before hark has ever
-- ingested that episode (or for a show hark doesn't track at all) —
-- resolving to episode_id is a query-time join, not a storage-time one.
-- Consumed by M4's scoring calibration; nothing reads this table yet.
CREATE TABLE IF NOT EXISTS listen_actions (
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
CREATE INDEX IF NOT EXISTS idx_listen_actions_episode_url ON listen_actions (episode_url);

-- gpodder_server.py's own subscription add/remove history (hark as
-- *server* — AntennaPod POSTs here directly), timestamped so a `since=`
-- replay (subscription_changes_since) can return only what changed after a
-- given point, matching the protocol AntennaPod's client expects. Distinct
-- from `shows` itself, which only holds current state — this is the event
-- log a repeat sync needs to stay incremental.
CREATE TABLE IF NOT EXISTS subscription_changes (
    id          INTEGER PRIMARY KEY,
    feed_url    TEXT NOT NULL,
    action      TEXT NOT NULL,
    occurred_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subscription_changes_occurred ON subscription_changes (occurred_at);

-- Generic small key/value store for sync cursors — currently just the
-- GPodder Sync episode_action `since` timestamp (subscriptions are cheap
-- enough to full-fetch every sync instead of needing a cursor).
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# Columns added after 0.1.0; CREATE IF NOT EXISTS won't touch existing tables,
# so they are bolted on here for databases created by older versions.
_MIGRATIONS = (
    ("episodes", "extracted_at", "ALTER TABLE episodes ADD COLUMN extracted_at TEXT"),
    ("episodes", "chapters_url", "ALTER TABLE episodes ADD COLUMN chapters_url TEXT"),
    ("episodes", "chapters_scanned_at", "ALTER TABLE episodes ADD COLUMN chapters_scanned_at TEXT"),
    ("episodes", "transcript_path", "ALTER TABLE episodes ADD COLUMN transcript_path TEXT"),
    ("episodes", "llm_detected_at", "ALTER TABLE episodes ADD COLUMN llm_detected_at TEXT"),
    ("episodes", "cut_path", "ALTER TABLE episodes ADD COLUMN cut_path TEXT"),
    ("shows", "feed_token", "ALTER TABLE shows ADD COLUMN feed_token TEXT"),
    ("shows", "ad_stripping_enabled",
     "ALTER TABLE shows ADD COLUMN ad_stripping_enabled INTEGER NOT NULL DEFAULT 1"),
    ("shows", "topic_index_enabled",
     "ALTER TABLE shows ADD COLUMN topic_index_enabled INTEGER NOT NULL DEFAULT 1"),
    ("listen_actions", "started", "ALTER TABLE listen_actions ADD COLUMN started INTEGER"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)
            if (table, column) == ("shows", "topic_index_enabled"):
                _backfill_topic_index_enabled(conn)


def _backfill_topic_index_enabled(conn: sqlite3.Connection) -> None:
    """One-time only — deliberately runs inside _migrate()'s own "column
    doesn't exist yet" guard, not on every connect() like
    _backfill_feed_tokens, since re-running this on a later connect() would
    stomp an owner's manual re-enable from the show page.

    Shows added via the bare feed-URL path (resolve.add_show_by_feed_url —
    gpodder sync, OPML import, discover --add) have `query` equal to their
    own feed_url (no search term to record) — that's what distinguishes
    them retroactively from hark-resolve's hand-curated shows, whose
    `query` is a human-typed show name. Corrects the ALTER TABLE's own
    DEFAULT 1 for exactly those already-existing bare-row shows."""
    conn.execute("UPDATE shows SET topic_index_enabled = 0 WHERE query LIKE 'http%'")


def _backfill_feed_tokens(conn: sqlite3.Connection) -> None:
    """Every show needs a feed_token to serve /feed and /audio — including shows
    that existed before this column did, not just ones added going forward."""
    for row in conn.execute("SELECT id FROM shows WHERE feed_token IS NULL"):
        conn.execute(
            "UPDATE shows SET feed_token = ? WHERE id = ?", (secrets.token_urlsafe(24), row["id"])
        )


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    _backfill_feed_tokens(conn)
    conn.commit()
    return conn


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
