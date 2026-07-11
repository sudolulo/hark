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
# _backfill_feed_tokens, not just ones added after this feature.
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
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)


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
