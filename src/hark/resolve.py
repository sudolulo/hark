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
    """Resolve each name and upsert hits into shows. Misses are reported, not stored."""
    results = []
    for name in names:
        show = resolve_show(client, name)
        if show is not None:
            upsert_show(conn, show)
        results.append((name, show))
    conn.commit()
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
    otherwise violate shows.feed_url's own UNIQUE constraint)."""
    existing = conn.execute("SELECT id FROM shows WHERE feed_url = ?", (feed_url,)).fetchone()
    if existing is not None:
        return False
    conn.execute(
        "INSERT INTO shows (query, title, feed_url) VALUES (?, ?, ?)",
        (feed_url, title, feed_url),
    )
    conn.commit()
    return True
