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
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from feedgen.feed import FeedGenerator

from adscrub.cut import CUT_SOURCES

# Podcasting 2.0 namespace — for <podcast:chapters> in "mark, don't cut" mode.
_PODCAST_NS = "https://podcastindex.org/namespace/1.0"


def chapters_json(conn: sqlite3.Connection, episode_id: int) -> dict:
    """A Podcasting-2.0 chapters document marking this episode's ad spans, so a player (AntennaPod)
    shows ad boundaries the listener can skip — the alternative to hard-cutting. Only cuttable
    tiers are marked (the same spans `cut` would remove)."""
    ph = ",".join("?" * len(CUT_SOURCES))
    spans = conn.execute(
        f"SELECT start_second, end_second FROM ad_segments WHERE episode_id = ? "
        f"AND source IN ({ph}) ORDER BY start_second", (episode_id, *CUT_SOURCES)).fetchall()
    chapters: list[dict] = []
    for s in spans:
        chapters.append({"startTime": round(s["start_second"], 1), "title": "Advertisement"})
        chapters.append({"startTime": round(s["end_second"], 1), "title": "Content"})
    return {"version": "1.2.0", "chapters": chapters}


def _add_chapters_links(rss: bytes, chapters_url_by_guid: dict[str, str]) -> bytes:
    """Add a <podcast:chapters> link to each <item> whose guid is in the map. Done by parsing the
    feedgen output rather than string-splicing, so the result stays well-formed."""
    ET.register_namespace("podcast", _PODCAST_NS)
    root = ET.fromstring(rss)
    for item in root.iter("item"):
        guid_el = item.find("guid")
        guid = guid_el.text if guid_el is not None else None
        url = chapters_url_by_guid.get(guid) if guid else None
        if url:
            ch = ET.SubElement(item, f"{{{_PODCAST_NS}}}chapters")
            ch.set("url", url)
            ch.set("type", "application/json+chapters")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _parse_pubdate(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def feed_url(show: sqlite3.Row, base_url: str) -> str:
    """The token-gated URL a podcast player subscribes to instead of the
    original feed — shared with web.py's show page, which displays this
    same URL for the operator to copy into AntennaPod."""
    return f"{base_url}/feed/{show['id']}/{show['feed_token']}"


def _episodes_with_cuttable_ads(conn: sqlite3.Connection, episode_ids: list[int]) -> set[int]:
    """Of the given episodes, those with at least one ad span in a cuttable tier — i.e. the ones
    'chapters' mode would mark. Empty in/empty out (no `IN ()`)."""
    if not episode_ids:
        return set()
    eph = ",".join("?" * len(episode_ids))
    sph = ",".join("?" * len(CUT_SOURCES))
    return {r[0] for r in conn.execute(
        f"SELECT DISTINCT episode_id FROM ad_segments WHERE episode_id IN ({eph}) "
        f"AND source IN ({sph})", (*episode_ids, *CUT_SOURCES))}


def _entry_media(ep: sqlite3.Row, feed_token: str | None, base_url: str, chapters_mode: bool,
                 with_ads: set[int]) -> tuple[str | None, int, str | None]:
    """Resolve one episode's enclosure for a feed, honoring the show's cut vs chapters mode. In
    'cut' mode a locally-cut file (if present) is served; in 'chapters' mode — or when no cut
    exists — the original enclosure is served and, if the episode has cuttable ads, a
    <podcast:chapters> URL is returned so the player can skip them. Returns
    (audio_url, enclosure_length, chapters_url); chapters_url is None unless in chapters mode."""
    length = 0
    if not chapters_mode and ep["cut_path"] and feed_token:
        cut_path = Path(ep["cut_path"])
        audio_url = f"{base_url}/audio/{ep['id']}/{feed_token}{cut_path.suffix}"
        if cut_path.is_file():
            length = cut_path.stat().st_size
    else:
        audio_url = ep["audio_url"]
    chapters_url = None
    if chapters_mode and feed_token and ep["id"] in with_ads:
        chapters_url = f"{base_url}/chapters/{ep['id']}/{feed_token}.json"
    return audio_url, length, chapters_url


def build_feed(conn: sqlite3.Connection, show: sqlite3.Row, base_url: str) -> bytes:
    fg = FeedGenerator()
    fg.title(show["title"] or show["query"])
    fg.link(href=show["feed_url"] or feed_url(show, base_url), rel="self")
    fg.description(show["description"] or show["title"] or show["query"])
    if show["image_url"]:
        fg.image(show["image_url"])

    # "chapters" mode marks ads instead of removing them: serve the ORIGINAL audio and attach a
    # <podcast:chapters> link per episode that has ad spans, so the listener can skip them.
    chapters_mode = show["cut_mode"] == "chapters"
    episodes = conn.execute(
        "SELECT * FROM episodes WHERE show_id = ? ORDER BY pubdate DESC", (show["id"],)
    ).fetchall()
    with_ads = (_episodes_with_cuttable_ads(conn, [e["id"] for e in episodes])
                if chapters_mode else set())
    chapters_url_by_guid: dict[str, str] = {}
    for ep in episodes:
        audio_url, length, chapters_url = _entry_media(
            ep, show["feed_token"], base_url, chapters_mode, with_ads)
        if not audio_url:
            continue  # nothing playable to link — skip rather than emit a dead enclosure
        if chapters_url and ep["guid"]:
            chapters_url_by_guid[ep["guid"]] = chapters_url
        fe = fg.add_entry()
        fe.id(ep["guid"])
        fe.title(ep["title"] or "(untitled)")
        fe.description(ep["description"] or "")
        pubdate = _parse_pubdate(ep["pubdate"])
        if pubdate:
            fe.pubDate(pubdate)
        fe.enclosure(audio_url, length, "audio/mpeg")

    rss = fg.rss_str(pretty=True)
    return _add_chapters_links(rss, chapters_url_by_guid) if chapters_url_by_guid else rss


def build_recommendation_feed(conn: sqlite3.Connection, username: str, base_url: str,
                              episode_ids: list[int], feed_token: str) -> bytes:
    """A personalized 'recommended for you' RSS feed (from scoring.py's ranking), subscribable in
    a podcast app like any other. Each episode is served the same way its own show's feed is: cut
    where a cut exists, or — if the show is in 'chapters' mode — the original audio with a
    <podcast:chapters> link, so a recommendation respects the show's mark-don't-cut choice. Order
    follows the ranking, not pubdate."""
    fg = FeedGenerator()
    fg.title(f"hark — recommended for {username}")
    fg.link(href=f"{base_url}/recommended/{feed_token}", rel="self")
    fg.description(f"Personalized episode recommendations for {username}, ad-stripped where available.")
    if not episode_ids:
        return fg.rss_str(pretty=True)
    placeholders = ",".join("?" * len(episode_ids))
    rows = conn.execute(
        f"SELECT e.id, e.guid, e.title, e.description, e.pubdate, e.audio_url, e.cut_path, "
        f"       s.feed_token, s.cut_mode, COALESCE(s.title, s.query) AS show "
        f"FROM episodes e JOIN shows s ON s.id = e.show_id WHERE e.id IN ({placeholders})",
        tuple(episode_ids)).fetchall()
    by_id = {r["id"]: r for r in rows}
    with_ads = _episodes_with_cuttable_ads(
        conn, [r["id"] for r in rows if r["cut_mode"] == "chapters"])
    chapters_url_by_guid: dict[str, str] = {}
    for eid in episode_ids:                       # preserve the recommendation ranking order
        ep = by_id.get(eid)
        if ep is None:
            continue
        chapters_mode = ep["cut_mode"] == "chapters"
        audio_url, length, chapters_url = _entry_media(
            ep, ep["feed_token"], base_url, chapters_mode, with_ads)
        if not audio_url:
            continue
        guid = ep["guid"] or f"hark-rec-{ep['id']}"
        if chapters_url:
            chapters_url_by_guid[guid] = chapters_url
        fe = fg.add_entry()
        fe.id(guid)
        fe.title(f"{ep['title'] or '(untitled)'} — {ep['show']}")
        fe.description(ep["description"] or "")
        pubdate = _parse_pubdate(ep["pubdate"])
        if pubdate:
            fe.pubDate(pubdate)
        fe.enclosure(audio_url, length, "audio/mpeg")
    rss = fg.rss_str(pretty=True)
    return _add_chapters_links(rss, chapters_url_by_guid) if chapters_url_by_guid else rss
