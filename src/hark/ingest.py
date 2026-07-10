"""RSS ingest: fetch resolved feeds, parse episodes, upsert into SQLite.

Re-runs are idempotent: unchanged episodes are left alone, changed ones are
updated in place, new ones inserted. Episodes are keyed by (show_id, guid).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

import feedparser
import httpx

from .db import utcnow

# Fields compared to decide whether an existing episode row needs an update.
_EPISODE_FIELDS = ("title", "description", "pubdate", "duration_seconds", "audio_url")


@dataclass
class ParsedEpisode:
    guid: str
    title: str | None
    description: str | None
    pubdate: str | None
    duration_seconds: int | None
    audio_url: str | None


@dataclass
class ParsedFeed:
    title: str | None
    description: str | None
    image_url: str | None
    episodes: list[ParsedEpisode]


@dataclass
class IngestResult:
    show_id: int
    query: str
    inserted: int = 0
    updated: int = 0
    total: int = 0
    error: str | None = None


def parse_duration(value) -> int | None:
    """Parse itunes:duration — plain seconds, MM:SS, or HH:MM:SS."""
    if value is None:
        return None
    parts = str(value).strip().split(":")
    try:
        nums = [int(float(p)) for p in parts]
    except ValueError:
        return None
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def _audio_url(entry) -> str | None:
    for enclosure in entry.get("enclosures", []):
        href = enclosure.get("href")
        mime = enclosure.get("type") or ""
        if href and (not mime or mime.startswith("audio/")):
            return href
    return None


def _pubdate(entry) -> str | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", parsed)
    return None


def parse_feed(content: bytes | str) -> ParsedFeed:
    parsed = feedparser.parse(content)
    episodes = []
    for entry in parsed.entries:
        audio_url = _audio_url(entry)
        guid = entry.get("id") or audio_url or entry.get("link")
        if not guid:
            continue
        episodes.append(
            ParsedEpisode(
                guid=guid,
                title=entry.get("title"),
                description=entry.get("summary"),
                pubdate=_pubdate(entry),
                duration_seconds=parse_duration(entry.get("itunes_duration")),
                audio_url=audio_url,
            )
        )
    feed = parsed.feed
    return ParsedFeed(
        title=feed.get("title"),
        description=feed.get("description") or feed.get("subtitle"),
        image_url=feed.get("image", {}).get("href"),
        episodes=episodes,
    )


def upsert_episodes(
    conn: sqlite3.Connection, show_id: int, episodes: list[ParsedEpisode]
) -> tuple[int, int]:
    """Insert new episodes, update changed ones. Returns (inserted, updated)."""
    existing = {
        row["guid"]: row
        for row in conn.execute("SELECT * FROM episodes WHERE show_id = ?", (show_id,))
    }
    inserted = updated = 0
    seen: set[str] = set()
    for ep in episodes:
        if ep.guid in seen:
            continue
        seen.add(ep.guid)
        row = existing.get(ep.guid)
        if row is None:
            conn.execute(
                """
                INSERT INTO episodes
                    (show_id, guid, title, description, pubdate, duration_seconds, audio_url)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (show_id, ep.guid, ep.title, ep.description, ep.pubdate,
                 ep.duration_seconds, ep.audio_url),
            )
            inserted += 1
        elif any(row[field] != getattr(ep, field) for field in _EPISODE_FIELDS):
            conn.execute(
                """
                UPDATE episodes
                SET title = ?, description = ?, pubdate = ?, duration_seconds = ?,
                    audio_url = ?, updated_at = ?
                WHERE id = ?
                """,
                (ep.title, ep.description, ep.pubdate, ep.duration_seconds,
                 ep.audio_url, utcnow(), row["id"]),
            )
            updated += 1
    return inserted, updated


def ingest_show(
    conn: sqlite3.Connection, client: httpx.Client, show: sqlite3.Row
) -> IngestResult:
    result = IngestResult(show_id=show["id"], query=show["query"])
    try:
        resp = client.get(show["feed_url"])
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        result.error = str(exc)
        return result
    parsed = parse_feed(resp.content)
    result.inserted, result.updated = upsert_episodes(conn, show["id"], parsed.episodes)
    result.total = len(parsed.episodes)
    conn.execute(
        """
        UPDATE shows
        SET title = COALESCE(?, title),
            description = COALESCE(?, description),
            image_url = COALESCE(?, image_url),
            last_fetched_at = ?
        WHERE id = ?
        """,
        (parsed.title, parsed.description, parsed.image_url, utcnow(), show["id"]),
    )
    conn.commit()
    return result


def ingest_all(conn: sqlite3.Connection, client: httpx.Client) -> list[IngestResult]:
    shows = conn.execute(
        "SELECT * FROM shows WHERE feed_url IS NOT NULL ORDER BY id"
    ).fetchall()
    return [ingest_show(conn, client, show) for show in shows]
