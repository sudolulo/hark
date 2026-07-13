"""Read-only queries over hark.db shared across the dashboard/topic/show
pages — split out of web.py (2026-07-13) as that module grew past ~1900
lines mixing these with auth, HTML templates, and HTTP routing.
"""

from __future__ import annotations

import json
import sqlite3

PAGE_SIZE = 50


def _topics_filter(genre: str, q: str) -> tuple[str, list]:
    if genre:
        return " WHERE t.id IN (SELECT topic_id FROM topic_genres WHERE genre = ?)", [genre]
    if q:
        return " WHERE (t.label LIKE ? COLLATE NOCASE OR t.wikidata_id = ?)", [f"%{q}%", q]
    return "", []


def topics_query(genre: str = "", q: str = "", limit: int | None = None,
                  offset: int = 0) -> tuple[str, tuple]:
    """Build the shared topic-listing query: base coverage stats, optionally
    filtered by genre or a label/QID search term, optionally paginated."""
    sql = """
        SELECT t.id, t.label, t.wikidata_id,
               COUNT(DISTINCT et.episode_id) AS episodes,
               COUNT(DISTINCT e.show_id) AS shows,
               COALESCE(GROUP_CONCAT(DISTINCT tg.genre), '') AS genres
        FROM topics t
        JOIN episode_topics et ON et.topic_id = t.id
        JOIN episodes e ON e.id = et.episode_id
        LEFT JOIN topic_genres tg ON tg.topic_id = t.id
    """
    where, params = _topics_filter(genre, q)
    sql += where + " GROUP BY t.id ORDER BY shows DESC, episodes DESC, t.label"
    if limit:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    return sql, tuple(params)


def topics_count(genre: str = "", q: str = "") -> tuple[str, tuple]:
    """Total topics matching the same filter as topics_query, for pagination.
    The filter only ever references columns on `t`, so no join is needed —
    unlike topics_query, which joins to compute per-topic episode/show counts."""
    where, params = _topics_filter(genre, q)
    return f"SELECT COUNT(*) FROM topics t{where}", tuple(params)


def related_topics(conn: sqlite3.Connection, topic_id: int, limit: int = 8) -> list[sqlite3.Row]:
    """Other topics ranked by how many episodes mention both — e.g. Fred West
    and Rosemary West co-occur across the same 3-part case file. Same
    co-occurrence idiom as related_shows, one level down (topic-to-topic
    instead of show-to-show)."""
    return conn.execute(
        """
        SELECT t2.id, t2.label, COUNT(DISTINCT et1.episode_id) AS episodes
        FROM episode_topics et1
        JOIN episode_topics et2
            ON et2.episode_id = et1.episode_id AND et2.topic_id != et1.topic_id
        JOIN topics t2 ON t2.id = et2.topic_id
        WHERE et1.topic_id = ?
        GROUP BY t2.id
        ORDER BY episodes DESC, t2.label
        LIMIT ?
        """,
        (topic_id, limit),
    ).fetchall()


def related_shows(conn: sqlite3.Connection, show_id: int, limit: int = 5) -> list[sqlite3.Row]:
    """Other shows ranked by how many topics they share with this one.

    M2's discovery milestone called for embedding similarity; this is a
    co-occurrence stand-in using data already on hand (topics + genres from
    M1 extraction) instead of standing up an embedding model/API key just
    for this. Good enough to be useful now; revisit if it's ever limiting.
    """
    return conn.execute(
        """
        SELECT s2.id, COALESCE(s2.title, s2.query) AS name,
               COUNT(DISTINCT et1.topic_id) AS shared
        FROM episode_topics et1
        JOIN episodes e1 ON e1.id = et1.episode_id
        JOIN episode_topics et2 ON et2.topic_id = et1.topic_id
        JOIN episodes e2 ON e2.id = et2.episode_id AND e2.show_id != e1.show_id
        JOIN shows s2 ON s2.id = e2.show_id
        WHERE e1.show_id = ?
        GROUP BY s2.id
        ORDER BY shared DESC, name
        LIMIT ?
        """,
        (show_id, limit),
    ).fetchall()


def contested_topics(conn: sqlite3.Connection, limit: int = 15) -> list[dict]:
    """Topics with a loaded claims comparison, ranked by how many claims are
    unique to one show rather than shared — a proxy for "the shows actually
    disagree/diverge here," distinct from the home page's cross-show
    *coverage* ranking. shared/unique_by_show are stored as JSON text (see
    claims.py); counted in Python rather than via SQLite JSON functions to
    match how the rest of the codebase already handles these columns.
    Read-only-connection-safe: topic_comparisons may not exist yet on a
    database no `hark compare`/`load-comparisons` has ever run against —
    same concern claims.count_pending_topics() already documents."""
    try:
        rows = conn.execute(
            """
            SELECT tc.topic_id, t.label, tc.shared, tc.unique_by_show,
                   COALESCE(GROUP_CONCAT(DISTINCT tg.genre), '') AS genres
            FROM topic_comparisons tc
            JOIN topics t ON t.id = tc.topic_id
            LEFT JOIN topic_genres tg ON tg.topic_id = tc.topic_id
            GROUP BY tc.topic_id
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    scored = []
    for r in rows:
        shared_count = len(json.loads(r["shared"]))
        unique_count = sum(len(v) for v in json.loads(r["unique_by_show"]).values())
        scored.append({
            "topic_id": r["topic_id"], "label": r["label"],
            "shared_count": shared_count, "unique_count": unique_count,
            "genres": [g for g in r["genres"].split(",") if g],
        })
    scored.sort(key=lambda s: s["unique_count"], reverse=True)
    return scored[:limit]


def rare_genre_episodes(conn: sqlite3.Connection, limit: int = 15) -> tuple[list[str], list[sqlite3.Row]]:
    """Episodes covering topics in hark's two least-common genres (by total
    topic count) — a rarity signal the home page's popularity-sorted tables
    don't surface. Returns the genre names picked (for the page's own
    explanatory text) alongside the episode rows."""
    genre_counts = conn.execute(
        "SELECT genre, COUNT(DISTINCT topic_id) AS n FROM topic_genres GROUP BY genre ORDER BY n ASC"
    ).fetchall()
    if not genre_counts:
        return [], []
    rare_genres = [r["genre"] for r in genre_counts[:2]]
    placeholders = ",".join("?" * len(rare_genres))
    rows = conn.execute(
        f"""
        SELECT e.id, e.title, s.id AS show_id, COALESCE(s.title, s.query) AS show,
               t.id AS topic_id, t.label, tg.genre
        FROM topic_genres tg
        JOIN topics t ON t.id = tg.topic_id
        JOIN episode_topics et ON et.topic_id = t.id
        JOIN episodes e ON e.id = et.episode_id
        JOIN shows s ON s.id = e.show_id
        WHERE tg.genre IN ({placeholders})
        ORDER BY et.confidence DESC NULLS LAST, e.pubdate DESC
        LIMIT ?
        """,
        (*rare_genres, limit),
    ).fetchall()
    return rare_genres, rows


def paginate(params: dict) -> int:
    """Parse ?page=N from query params, clamped to >= 1."""
    try:
        return max(1, int(params.get("page", ["1"])[0]))
    except ValueError:
        return 1
