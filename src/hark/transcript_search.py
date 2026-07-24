"""Full-text search over episode transcripts, via SQLite FTS5.

Transcripts are stored as JSON files (episodes.transcript_path), not in the DB, so title/topic
search never reached inside them. This builds a `transcript_fts` FTS5 index of the transcript
TEXT, populated incrementally by the `index-transcripts` pipeline stage (like the fingerprint
index) and queried by `/search`.

The search path is read-only-safe: it never creates the table (the web app's connection can't
write), degrading to "no matches" via a guarded query — same discipline as queries.pipeline_status.
Indexing (the write path) runs only from the CLI/pipeline, which has a writable connection.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import queries

# episode_id UNINDEXED: stored for retrieval but not tokenised (we match on `text` only).
_CREATE = "CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(text, episode_id UNINDEXED)"


def fts5_available(conn: sqlite3.Connection) -> bool:
    """Whether this SQLite build has FTS5 (it normally does). Used to fail soft rather than crash
    indexing on an FTS5-less build."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE)
    conn.commit()


def pending_index_ids(conn: sqlite3.Connection) -> list[int]:
    """Transcribed episodes not yet in the FTS index — the incremental index queue."""
    ensure_schema(conn)
    have = {r[0] for r in conn.execute("SELECT DISTINCT episode_id FROM transcript_fts")}
    return [eid for (eid,) in conn.execute(
        "SELECT id FROM episodes WHERE transcript_path IS NOT NULL ORDER BY id") if eid not in have]


def _transcript_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    segs = data.get("segments", data) if isinstance(data, dict) else data
    if not isinstance(segs, list):
        return None
    return " ".join(s.get("text", "").strip() for s in segs if isinstance(s, dict))


def index_transcripts(conn: sqlite3.Connection, limit: int | None = None) -> tuple[int, int]:
    """Index up to `limit` un-indexed transcripts into FTS. Returns (indexed, still_pending).
    A missing/unreadable transcript is skipped (not fatal) but still marked done — its `id` goes
    in with empty text — so a lost file doesn't get retried forever."""
    if not fts5_available(conn):
        return (0, 0)
    ensure_schema(conn)
    pending = pending_index_ids(conn)
    todo = pending[:limit] if limit else pending
    indexed = 0
    for eid in todo:
        row = conn.execute("SELECT transcript_path FROM episodes WHERE id = ?", (eid,)).fetchone()
        text = _transcript_text(row[0]) if row and row[0] else None
        conn.execute("INSERT INTO transcript_fts (episode_id, text) VALUES (?, ?)", (eid, text or ""))
        if text:
            indexed += 1
    conn.commit()
    return (indexed, len(pending) - len(todo))


def search(conn: sqlite3.Connection, query: str, limit: int = 25,
           genre: str = "") -> list[sqlite3.Row]:
    """Episodes whose transcript matches `query`, with a context snippet. Read-only-safe: guarded
    so a database without the FTS table (fresh deploy) yields no matches instead of erroring. The
    query is matched as a quoted PHRASE, which both does the intuitive thing and sidesteps FTS5's
    own query-operator syntax erroring on stray punctuation. `genre`, if given, scopes hits to
    episodes whose topics fall in that genre — same filter /search applies to topics and titles."""
    q = query.strip()
    if not q:
        return []
    phrase = '"' + q.replace('"', '""') + '"'
    genre_clause, genre_params = queries.episode_in_genre(genre)
    if genre_clause:
        genre_clause = "AND " + genre_clause + " "
    try:
        return conn.execute(
            "SELECT e.id, e.title, s.id AS show_id, COALESCE(s.title, s.query) AS show, "
            "       snippet(transcript_fts, 0, '', '', '…', 14) AS snip "
            "FROM transcript_fts f "
            "JOIN episodes e ON e.id = f.episode_id "
            "JOIN shows s ON s.id = e.show_id "
            "WHERE transcript_fts MATCH ? " + genre_clause
            + "ORDER BY rank LIMIT ?",
            (phrase, *genre_params, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
