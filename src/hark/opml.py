"""OPML import: a manual fallback for adding shows by feed URL when a
podcast app's OPML export is what you have, rather than (or in addition to)
the Nextcloud gpodder sync in nextcloud.py — same underlying
resolve.add_show_by_feed_url() either way. Stdlib XML only; OPML's
`<outline type="rss" xmlUrl="..." text="...">` shape doesn't need a real
parser.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass
class OpmlEntry:
    feed_url: str
    title: str | None


def parse_opml(content: str | bytes) -> list[OpmlEntry]:
    root = ET.fromstring(content)
    entries = []
    for outline in root.iter("outline"):
        feed_url = outline.get("xmlUrl")
        if not feed_url:
            continue  # a folder/grouping outline, not a feed
        title = outline.get("title") or outline.get("text")
        entries.append(OpmlEntry(feed_url=feed_url, title=title))
    return entries


def read_opml_file(path: str | Path) -> list[OpmlEntry]:
    return parse_opml(Path(path).read_bytes())
