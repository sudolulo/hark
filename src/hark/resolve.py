"""Resolve show names from feeds.txt to feed URLs via the keyless iTunes Search API."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx

from .db import utcnow

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"


@dataclass
class ResolvedShow:
    query: str
    title: str
    feed_url: str
    itunes_id: int | None = None
    author: str | None = None
    image_url: str | None = None


@dataclass
class BackfillResult:
    show_id: int
    query: str
    itunes_id: int | None = None
    error: str | None = None


def read_feeds_file(path: str | Path) -> list[str]:
    """Return show names from feeds.txt, skipping blanks and # comments."""
    names = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names


def search_podcasts(client: httpx.Client, term: str, limit: int = 5) -> list[dict]:
    resp = client.get(
        ITUNES_SEARCH_URL,
        params={"media": "podcast", "entity": "podcast", "term": term, "limit": limit},
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def resolve_show(client: httpx.Client, name: str) -> ResolvedShow | None:
    """Best match for a show name: the first search result that has a feed URL."""
    for result in search_podcasts(client, name):
        feed_url = result.get("feedUrl")
        if feed_url:
            return ResolvedShow(
                query=name,
                title=result.get("collectionName") or name,
                feed_url=feed_url,
                itunes_id=result.get("collectionId"),
                author=result.get("artistName"),
                image_url=result.get("artworkUrl600"),
            )
    return None


def upsert_show(conn: sqlite3.Connection, show: ResolvedShow) -> None:
    conn.execute(
        """
        INSERT INTO shows (query, title, feed_url, itunes_id, author, image_url)
        VALUES (:query, :title, :feed_url, :itunes_id, :author, :image_url)
        ON CONFLICT (query) DO UPDATE SET
            title = excluded.title,
            feed_url = excluded.feed_url,
            itunes_id = excluded.itunes_id,
            author = excluded.author,
            image_url = excluded.image_url,
            updated_at = :now
        """,
        {
            "query": show.query,
            "title": show.title,
            "feed_url": show.feed_url,
            "itunes_id": show.itunes_id,
            "author": show.author,
            "image_url": show.image_url,
            "now": utcnow(),
        },
    )


def resolve_all(
    conn: sqlite3.Connection, client: httpx.Client, names: list[str]
) -> list[tuple[str, ResolvedShow | None]]:
    """Resolve each name and upsert hits into shows. Misses (including a
    network/API failure for that one name) are reported as (name, None), not
    stored. Commits after every hit — every other batch command in this
    codebase (extract, transcribe, detect-ads, compare) isolates failures per
    item so one bad item doesn't lose prior progress; this used to commit
    only once at the very end, so a single feeds.txt entry raising partway
    through a real run (a network blip against the iTunes Search API) would
    silently roll back every show already resolved before it."""
    results = []
    for name in names:
        try:
            show = resolve_show(client, name)
        except httpx.HTTPError:
            show = None
        if show is not None:
            upsert_show(conn, show)
            conn.commit()
        results.append((name, show))
    return results


def backfill_itunes_ids(
    conn: sqlite3.Connection, client: httpx.Client, limit: int | None = None
) -> list[BackfillResult]:
    """Fill in itunes_id for shows that don't have one yet. itunes_id is only
    ever set by resolve_show()'s hand-curated path — add_show_by_feed_url()
    (gpodder sync, OPML import, discover --add) never sets it, and that's how
    a large fraction of a real catalog actually gets registered. It's worth
    having as a second, URL-drift-proof match key beyond feed_url alone: a
    feed can move, and an external service (e.g. a ratings source) may have
    indexed a different canonical URL than the one hark has stored.

    Searches by title/query as a candidate generator only — a result is
    accepted ONLY when its own feedUrl exactly matches the show's stored
    feed_url, which is what actually verifies the match. A bare
    title-similarity accept could misattribute the wrong show's id entirely;
    exact feed URL equality can't. No match found this run just leaves
    itunes_id NULL to retry next time — this is the keyless iTunes Search API,
    so unlike the ratings source there's no query budget to conserve."""
    sql = "SELECT id, query, title, feed_url FROM shows WHERE itunes_id IS NULL AND feed_url IS NOT NULL ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        rows = conn.execute(sql, (limit,)).fetchall()
    else:
        rows = conn.execute(sql).fetchall()

    results = []
    for row in rows:
        result = BackfillResult(show_id=row["id"], query=row["query"])
        try:
            hits = search_podcasts(client, row["title"] or row["query"])
        except httpx.HTTPError as exc:
            result.error = str(exc)
            results.append(result)
            continue
        match = next((h for h in hits if h.get("feedUrl") == row["feed_url"]), None)
        itunes_id = match.get("collectionId") if match else None
        if itunes_id:
            conn.execute("UPDATE shows SET itunes_id = ? WHERE id = ?", (itunes_id, row["id"]))
            conn.commit()
            result.itunes_id = itunes_id
        results.append(result)
    return results


def add_show_by_feed_url(conn: sqlite3.Connection, feed_url: str, title: str | None = None) -> bool:
    """Register a show hark already has a direct feed URL for — gpodder
    subscription sync and OPML import both land here instead of going
    through resolve_show()'s iTunes Search lookup, since that's only needed
    to turn a *name* into a feed URL and these already have one.

    `query` (shows' actual UNIQUE key) is set to the feed URL itself: there's
    no search term to record, and a feed URL is guaranteed unique the same
    way a real one is. title/description/image are left for the next `hark
    ingest` to fill in from the feed itself — same as any other show — except
    OPML's own <outline text="..."> is used as a first-pass title when given,
    so a freshly-imported show isn't nameless until the next ingest run.

    Returns False (no-op) if a show with this feed_url already exists —
    including one added via resolve_show()'s iTunes path, which is why this
    checks feed_url rather than relying on the query-column ON CONFLICT that
    upsert_show() uses (a different query value for the same feed_url would
    otherwise violate shows.feed_url's own UNIQUE constraint).

    topic_index_enabled starts OFF here (unlike upsert_show()'s hand-curated
    path, which keeps the schema default of on) — nothing has reviewed
    whether this show is even genre-relevant yet. Toggle it on from the show
    page once it is. See db.py's schema comment for the full reasoning."""
    existing = conn.execute("SELECT id FROM shows WHERE feed_url = ?", (feed_url,)).fetchone()
    if existing is not None:
        return False
    conn.execute(
        "INSERT INTO shows (query, title, feed_url, topic_index_enabled) VALUES (?, ?, ?, 0)",
        (feed_url, title, feed_url),
    )
    conn.commit()
    return True
