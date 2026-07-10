"""SQLite storage: schema and connection helper."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# topics / topic_genres / episode_topics are created now but only populated by
# M1 extraction; wikidata_id, confidence, and source stay NULL until then.
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
    last_fetched_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS episodes (
    id               INTEGER PRIMARY KEY,
    show_id          INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    guid             TEXT NOT NULL,
    title            TEXT,
    description      TEXT,
    pubdate          TEXT,
    duration_seconds INTEGER,
    audio_url        TEXT,
    extracted_at     TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at       TEXT,
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

CREATE INDEX IF NOT EXISTS idx_episodes_show_pubdate ON episodes (show_id, pubdate);
"""


# Columns added after 0.1.0; CREATE IF NOT EXISTS won't touch existing tables,
# so they are bolted on here for databases created by older versions.
_MIGRATIONS = (
    ("episodes", "extracted_at", "ALTER TABLE episodes ADD COLUMN extracted_at TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
