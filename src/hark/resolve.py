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
