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
    hosting_platform TEXT,
    last_fetched_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at      TEXT
);

-- M4: cached external show ratings (e.g. Taddy) — a source, not a merge
-- of rating data into `shows` itself, since (a) more sources can be added
-- later without a migration and (b) a row records a *fetch attempt*, not
-- just a hit: written even on no-match/zero-review (external_id/rating_avg/
-- rating_count left NULL, fetched_at still set) so a show the source
-- doesn't have isn't re-queried against a rate-limited API's budget on
-- every run — same idiom pipeline.py's extracted_at already uses for a
-- zero-topic episode. Composite natural key + cascade, same shape as
-- topic_genres/episode_topics.
CREATE TABLE IF NOT EXISTS show_ratings (
    show_id      INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    source       TEXT NOT NULL,
    external_id  TEXT,
    rating_avg   REAL,
    rating_count INTEGER,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (show_id, source)
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
    audio_gone_at       TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at          TEXT,
    UNIQUE (show_id, guid)
);

-- Dual-fetch dynamic-ad-insertion probes (2026-07-14 research): does re-fetching
-- an episode's audio_url with a different listener-targeting signal (cookie +
-- user-agent) actually return different stitched content? A row records an
-- ATTEMPT (tested_at always set), same idiom as show_ratings — a platform that
-- never varies is exactly as useful to know as one that does, and re-probing it
-- forever would waste bandwidth for nothing. `platform` is a copy of the show's
-- hosting_platform at test time, not a join, so historical probes stay readable
-- even if platform detection logic or a show's actual host later changes.
CREATE TABLE IF NOT EXISTS dai_probes (
    id                    INTEGER PRIMARY KEY,
    episode_id            INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    platform              TEXT,
    tested_at             TEXT NOT NULL,
    bytes_compared        INTEGER NOT NULL,
    diverged              INTEGER NOT NULL,
    divergence_byte       INTEGER,
    divergence_second     REAL,
    reconverged           INTEGER,
    reconvergence_byte    INTEGER,
    reconvergence_second  REAL
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
-- Consumed by M4's scoring.py (0.17.0) for personal genre/topic affinity.
-- user_id (multi-user, 0.14.0) is part of the UNIQUE constraint, not just a
-- tag column: play position is inherently personal, so two accounts playing
-- the same episode at the same occurred_at must not collide/dedupe against
-- each other the way two AntennaPod installs on ONE account correctly do.
CREATE TABLE IF NOT EXISTS listen_actions (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL DEFAULT 1,
    podcast_url  TEXT NOT NULL,
    episode_url  TEXT NOT NULL,
    episode_guid TEXT,
    action       TEXT NOT NULL,
    started      INTEGER,
    position     INTEGER,
    total        INTEGER,
    occurred_at  TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (user_id, podcast_url, episode_url, action, occurred_at)
);
CREATE INDEX IF NOT EXISTS idx_listen_actions_episode_url ON listen_actions (episode_url);

-- gpodder_server.py's own subscription add/remove history (hark as
-- *server* — AntennaPod POSTs here directly), timestamped so a `since=`
-- replay (subscription_changes_since) can return only what changed after a
-- given point, matching the protocol AntennaPod's client expects. Distinct
-- from `shows` itself, which only holds current state — this is the event
-- log a repeat sync needs to stay incremental. user_id scopes it per account
-- (multi-user, 0.14.0) — see user_shows below for *why* a separate table.
CREATE TABLE IF NOT EXISTS subscription_changes (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL DEFAULT 1,
    feed_url    TEXT NOT NULL,
    action      TEXT NOT NULL,
    occurred_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subscription_changes_occurred ON subscription_changes (occurred_at);

-- Multi-user (0.14.0): a user's personal subscription list — current state,
-- same relationship to subscription_changes' event log that `shows` has to
-- `episodes`' ingest history. Deliberately NOT a user_id column on `shows`
-- itself: shows/episodes/transcripts/ad_segments all stay global and shared
-- across every account, which is what avoids re-transcribing/re-detecting
-- the same episode once per subscriber. user_id has no FK — `users` lives in
-- the separate auth.db (see web.py), by the same design that keeps sessions
-- surviving a hark.db data-snapshot restore; this is a soft cross-database
-- reference by convention, same category as feed_token/toggles already
-- being hark.db-resident config that doesn't travel with a snapshot swap.
CREATE TABLE IF NOT EXISTS user_shows (
    user_id  INTEGER NOT NULL,
    show_id  INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (user_id, show_id)
);
CREATE INDEX IF NOT EXISTS idx_user_shows_user ON user_shows (user_id);

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
    ("episodes", "audio_gone_at", "ALTER TABLE episodes ADD COLUMN audio_gone_at TEXT"),
    ("shows", "feed_token", "ALTER TABLE shows ADD COLUMN feed_token TEXT"),
    ("shows", "ad_stripping_enabled",
     "ALTER TABLE shows ADD COLUMN ad_stripping_enabled INTEGER NOT NULL DEFAULT 1"),
    ("shows", "topic_index_enabled",
     "ALTER TABLE shows ADD COLUMN topic_index_enabled INTEGER NOT NULL DEFAULT 1"),
    ("shows", "hosting_platform", "ALTER TABLE shows ADD COLUMN hosting_platform TEXT"),
    ("listen_actions", "started", "ALTER TABLE listen_actions ADD COLUMN started INTEGER"),
    ("subscription_changes", "user_id",
     "ALTER TABLE subscription_changes ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)
            if (table, column) == ("shows", "topic_index_enabled"):
                _backfill_topic_index_enabled(conn)
    # Run after the loop above (not folded into _MIGRATIONS): this needs a full
    # rebuild (new UNIQUE constraint), not a plain ADD COLUMN, and it depends on
    # the "started" column already existing (added by the loop, if missing) so
    # the copy-into-new-table SELECT below has something to select.
    _migrate_listen_actions_user_scoped(conn)


def _migrate_listen_actions_user_scoped(conn: sqlite3.Connection) -> None:
    """One-time rebuild: adds user_id to listen_actions AND to its UNIQUE
    constraint (multi-user, 0.14.0) — a plain ALTER TABLE ADD COLUMN can't
    change a UNIQUE constraint, so this does the rename/create/copy/drop
    dance instead. Existing rows backfill to user_id 1, the pre-existing
    bootstrap account (auth.db's Auth.__init__ always inserts it first) —
    see user_shows' own backfill in connect() for the same reasoning."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(listen_actions)")}
    if "user_id" in cols:
        return
    conn.execute("ALTER TABLE listen_actions RENAME TO listen_actions_old")
    conn.execute(
        """
        CREATE TABLE listen_actions (
            id           INTEGER PRIMARY KEY,
            user_id      INTEGER NOT NULL DEFAULT 1,
            podcast_url  TEXT NOT NULL,
            episode_url  TEXT NOT NULL,
            episode_guid TEXT,
            action       TEXT NOT NULL,
            started      INTEGER,
            position     INTEGER,
            total        INTEGER,
            occurred_at  TEXT,
            created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE (user_id, podcast_url, episode_url, action, occurred_at)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO listen_actions (id, user_id, podcast_url, episode_url, episode_guid,
                                     action, started, position, total, occurred_at, created_at)
        SELECT id, 1, podcast_url, episode_url, episode_guid, action, started, position,
               total, occurred_at, created_at
        FROM listen_actions_old
        """
    )
    conn.execute("DROP TABLE listen_actions_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listen_actions_episode_url ON listen_actions (episode_url)")


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


def _backfill_user_shows(conn: sqlite3.Connection) -> None:
    """One-time only, for databases that had shows before per-user
    subscription lists existed (multi-user, 0.14.0): gives the pre-existing
    bootstrap account (auth.db user id 1 — always the first row Auth.__init__
    inserts) visibility into every show that already existed, so upgrading
    doesn't blank out the one real account's dashboard. New shows only enter
    user_shows going forward via an explicit subscribe (gpodder sync or the
    web UI) — this never runs again once user_shows exists, so it can't stomp
    anyone's later unsubscribe."""
    conn.execute("INSERT OR IGNORE INTO user_shows (user_id, show_id) SELECT 1, id FROM shows")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Checked BEFORE executescript() creates it (CREATE TABLE IF NOT EXISTS
    # below would otherwise make this always true) — see _backfill_user_shows.
    user_shows_is_new = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'user_shows'"
    ).fetchone() is None
    conn.executescript(SCHEMA)
    _migrate(conn)
    if user_shows_is_new:
        _backfill_user_shows(conn)
    _backfill_feed_tokens(conn)
    conn.commit()
    return conn


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
