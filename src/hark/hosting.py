"""Classify a show's hosting platform from its episodes' audio_url, so DAI-probe
results (dai_probe.py) can be grouped by platform to see which ones actually
support the dual-fetch technique. Not an authoritative company-name lookup —
just the registrable domain a show's audio is actually served from, which is
what determines whether varying request signals can reach a real ad-server
targeting decision at all.
"""

from __future__ import annotations

import sqlite3
from urllib.parse import urlparse

# Domains with more than two labels in their public suffix (co.uk, com.au, ...)
# where the naive "last two labels" heuristic below would collapse distinct
# providers into the shared suffix (open.live.bbc.co.uk -> "co.uk"). Short and
# best-effort, not a full public-suffix-list implementation — not worth the
# dependency for what's a grouping label, not an authoritative identity.
_MULTI_PART_SUFFIXES = ("co.uk", "com.au", "co.nz", "org.uk")


def _registrable_domain(netloc: str) -> str | None:
    host = netloc.split(":")[0].lower()
    labels = host.split(".")
    if len(labels) < 2:
        return host or None
    last_three = ".".join(labels[-3:])
    for suffix in _MULTI_PART_SUFFIXES:
        if last_three.endswith(suffix) and len(labels) >= 3:
            return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def classify_platform(audio_url: str) -> str | None:
    """The registrable domain audio_url is actually served from, e.g.
    "https://sphinx.acast.com/p/.../media.mp3" -> "acast.com". None for a URL
    that doesn't parse to a usable host."""
    try:
        netloc = urlparse(audio_url).netloc
    except ValueError:
        return None
    return _registrable_domain(netloc) if netloc else None


def backfill_hosting_platform(conn: sqlite3.Connection) -> int:
    """Set shows.hosting_platform from the most recent episode's audio_url, for
    shows that don't have one yet. Returns the number of shows updated."""
    rows = conn.execute(
        """
        SELECT s.id AS show_id, e.audio_url
        FROM shows s
        JOIN episodes e ON e.show_id = s.id
        WHERE s.hosting_platform IS NULL AND e.audio_url IS NOT NULL
        GROUP BY s.id
        HAVING e.id = MAX(e.id)
        """
    ).fetchall()
    updated = 0
    for row in rows:
        platform = classify_platform(row["audio_url"])
        if platform is None:
            continue
        conn.execute(
            "UPDATE shows SET hosting_platform = ? WHERE id = ?", (platform, row["show_id"])
        )
        updated += 1
    conn.commit()
    return updated
