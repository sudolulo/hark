"""Build a cleaned RSS feed (feedgen) pointing at cut_path episodes.

This is the only integration point any podcast player needs: subscribe to
`/feed/<show_id>/<token>` instead of the original feed URL. Episodes with a
cut_path are served locally at `/audio/<episode_id>/<token>.<ext>`; everything
else still points at its original audio_url unchanged — an episode nobody has
cut (no ads found, or not processed yet) doesn't need a local copy at all.

The token gates both routes (see web.py) since a podcast app can't do the
dashboard's cookie-session login — see CLAUDE.md for why this is a per-show
token embedded in the URL rather than either fully open or a second auth
system. This is hark's own file, not imported from adscrub: adscrub's own
feed.py builds against its `feeds`/`feed_id` schema and has no token concept
at all, so there's no reusable piece here beyond feedgen itself (already a
direct hark dependency).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from feedgen.feed import FeedGenerator


def _parse_pubdate(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def feed_url(show: sqlite3.Row, base_url: str) -> str:
    """The token-gated URL a podcast player subscribes to instead of the
    original feed — shared with web.py's show page, which displays this
    same URL for the operator to copy into AntennaPod."""
    return f"{base_url}/feed/{show['id']}/{show['feed_token']}"


def build_feed(conn: sqlite3.Connection, show: sqlite3.Row, base_url: str) -> bytes:
    fg = FeedGenerator()
    fg.title(show["title"] or show["query"])
    fg.link(href=show["feed_url"] or feed_url(show, base_url), rel="self")
    fg.description(show["description"] or show["title"] or show["query"])
    if show["image_url"]:
        fg.image(show["image_url"])

    episodes = conn.execute(
        "SELECT * FROM episodes WHERE show_id = ? ORDER BY pubdate DESC", (show["id"],)
    ).fetchall()
    for ep in episodes:
        length = 0
        if ep["cut_path"]:
            cut_path = Path(ep["cut_path"])
            audio_url = f"{base_url}/audio/{ep['id']}/{show['feed_token']}{cut_path.suffix}"
            if cut_path.is_file():
                length = cut_path.stat().st_size
        else:
            audio_url = ep["audio_url"]
        if not audio_url:
            continue  # nothing playable to link — skip rather than emit a dead enclosure
        fe = fg.add_entry()
        fe.id(ep["guid"])
        fe.title(ep["title"] or "(untitled)")
        fe.description(ep["description"] or "")
        pubdate = _parse_pubdate(ep["pubdate"])
        if pubdate:
            fe.pubDate(pubdate)
        fe.enclosure(audio_url, length, "audio/mpeg")

    return fg.rss_str(pretty=True)


def build_recommendation_feed(conn: sqlite3.Connection, username: str, base_url: str,
                              episode_ids: list[int], feed_token: str) -> bytes:
    """A personalized 'recommended for you' RSS feed (from scoring.py's ranking), subscribable in
    a podcast app like any other. Episodes are served AD-STRIPPED where a cut exists (via the
    show's own feed_token), else the original enclosure — so recommendations get the same
    ad-removal as a subscribed show. Order follows the ranking, not pubdate."""
    fg = FeedGenerator()
    fg.title(f"hark — recommended for {username}")
    fg.link(href=f"{base_url}/recommended/{feed_token}", rel="self")
    fg.description(f"Personalized episode recommendations for {username}, ad-stripped where available.")
    if not episode_ids:
        return fg.rss_str(pretty=True)
    placeholders = ",".join("?" * len(episode_ids))
    rows = conn.execute(
        f"SELECT e.id, e.guid, e.title, e.description, e.pubdate, e.audio_url, e.cut_path, "
        f"       s.feed_token, COALESCE(s.title, s.query) AS show "
        f"FROM episodes e JOIN shows s ON s.id = e.show_id WHERE e.id IN ({placeholders})",
        tuple(episode_ids)).fetchall()
    by_id = {r["id"]: r for r in rows}
    for eid in episode_ids:                       # preserve the recommendation ranking order
        ep = by_id.get(eid)
        if ep is None:
            continue
        length = 0
        if ep["cut_path"] and ep["feed_token"]:
            cut_path = Path(ep["cut_path"])
            audio_url = f"{base_url}/audio/{ep['id']}/{ep['feed_token']}{cut_path.suffix}"
            if cut_path.is_file():
                length = cut_path.stat().st_size
        else:
            audio_url = ep["audio_url"]
        if not audio_url:
            continue
        fe = fg.add_entry()
        fe.id(ep["guid"] or f"hark-rec-{ep['id']}")
        fe.title(f"{ep['title'] or '(untitled)'} — {ep['show']}")
        fe.description(ep["description"] or "")
        pubdate = _parse_pubdate(ep["pubdate"])
        if pubdate:
            fe.pubDate(pubdate)
        fe.enclosure(audio_url, length, "audio/mpeg")
    return fg.rss_str(pretty=True)
