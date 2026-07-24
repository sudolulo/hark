"""Detect multi-part episode series from titles, so an episode page can link to its other parts.

Heuristic and title-only (no schema change): a lot of shows title multi-part coverage as
"… Part 2", "(Pt. 3)", or "(2 of 4)". We strip that marker to a base title and group episodes in
the SAME show that share it. Intentionally conservative — a single detected part isn't a series,
and cross-show matching is never attempted (two shows' "Part 1"s are unrelated).
"""
from __future__ import annotations

import re
import sqlite3

# "Part 2", "Pt. 3", "Part Two"(digits only), or "(2 of 4)" / "(2/4)". Case-insensitive.
_PART_RE = re.compile(
    r"\b(?:part|pt\.?)\s*(\d+)\b|\((\d+)\s*(?:of|/)\s*\d+\)", re.IGNORECASE)
_TRAILING = re.compile(r"[\s:\u2013\u2014\-|(),.]+$")   # trim dangling separators after stripping


def series_key(title: str | None) -> tuple[str, int] | None:
    """(base_title_lowercased, part_number) if `title` looks like one part of a multi-part
    series, else None."""
    if not title:
        return None
    m = _PART_RE.search(title)
    if not m:
        return None
    part = int(m.group(1) or m.group(2))
    base = _TRAILING.sub("", _PART_RE.sub("", title, count=1)).strip()
    return (base.lower(), part) if base else None


def siblings(conn: sqlite3.Connection, episode_id: int) -> list[dict]:
    """The episodes that make up this episode's series (including itself), ordered by part.
    Empty unless 2+ parts are found in the same show. Read-only."""
    row = conn.execute("SELECT show_id, title FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if row is None:
        return []
    key = series_key(row["title"])
    if key is None:
        return []
    base = key[0]
    found = []
    for e in conn.execute("SELECT id, title FROM episodes WHERE show_id = ?", (row["show_id"],)):
        k = series_key(e["title"])
        if k and k[0] == base:
            found.append({"id": e["id"], "title": e["title"], "part": k[1]})
    found.sort(key=lambda s: (s["part"], s["id"]))
    return found if len(found) > 1 else []
