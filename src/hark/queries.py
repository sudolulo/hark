"""Read-only queries over hark.db shared across the dashboard/topic/show
pages — split out of web.py (2026-07-13) as that module grew past ~1900
lines mixing these with auth, HTML templates, and HTTP routing.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

PAGE_SIZE = 50


def _topics_filter(genre: str, q: str) -> tuple[str, list]:
    if genre:
        return " WHERE t.id IN (SELECT topic_id FROM topic_genres WHERE genre = ?)", [genre]
    if q:
        return " WHERE (t.label LIKE ? COLLATE NOCASE OR t.wikidata_id = ?)", [f"%{q}%", q]
    return "", []


# Whitelisted sort keys only — never interpolate the raw `sort` query param
# into SQL. Each maps to a full ORDER BY clause; "shows" is the original
# (and still default) ordering.
_SORT_CLAUSES = {
    "shows": "shows DESC, episodes DESC, t.label",
    "episodes": "episodes DESC, shows DESC, t.label",
    "label": "t.label",
}


def topics_query(genre: str = "", q: str = "", sort: str = "shows", limit: int | None = None,
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
    order = _SORT_CLAUSES.get(sort, _SORT_CLAUSES["shows"])
    sql += where + f" GROUP BY t.id ORDER BY {order}"
    # is not None, not a truthy check: limit=0 must mean "zero rows" (SQL's
    # own LIMIT 0 semantics), not "no limit" — reachable via `hark topics
    # --limit 0`, the same falsy-zero class of bug already fixed in
    # claims.pending_topics() and cli._filter_enabled().
    if limit is not None:
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


def pipeline_status(conn: sqlite3.Connection) -> dict:
    """Everything the /pipeline dashboard needs: per-stage last-run/status rows (keyed by stage),
    ad spans per tier, fingerprint-library size, and quarantine/held counts.

    Every read is guarded (sqlite3.OperationalError): pipeline_runs is created and migrated by the
    orchestrator (the transcribe service), not the web app's read-only connection, so on a fresh
    deploy the table — or its status columns — may not exist yet. A missing piece degrades to an
    empty/zero value rather than 500-ing the page. Same discipline as contested_topics()."""
    out: dict = {"stages": {}, "spans": [], "library": 0, "quarantined": 0, "held": 0,
                 "spend": {"ads": 0.0, "comparisons": 0.0}}
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for r in conn.execute("SELECT category, dollars FROM llm_spend WHERE day = ?", (today,)):
            out["spend"][r["category"]] = r["dollars"]   # read-only: never touches llm_budget's ensure_schema
    except sqlite3.OperationalError:
        pass
    try:
        out["stages"] = {r["stage"]: r for r in conn.execute(
            "SELECT stage, last_run, last_status, last_seen, last_exit FROM pipeline_runs")}
    except sqlite3.OperationalError:
        pass
    try:
        out["spans"] = conn.execute(
            "SELECT source, COUNT(*) AS n FROM ad_segments GROUP BY source ORDER BY n DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        pass
    try:
        out["library"] = conn.execute("SELECT COUNT(*) FROM ad_fingerprints").fetchone()[0]
    except sqlite3.OperationalError:
        pass
    try:
        out["quarantined"] = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE audio_gone_at IS NOT NULL").fetchone()[0]
        out["held"] = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE cut_held_at IS NOT NULL").fetchone()[0]
    except sqlite3.OperationalError:
        pass
    return out


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
