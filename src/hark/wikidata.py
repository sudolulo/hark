"""Canonicalize extracted topic labels against Wikidata.

wbsearchentities is keyless and alias-aware: searching "BTK" and "Dennis
Rader" both land on Q2295394, which is what lets hark merge alias topics
into one row. A per-instance cache keeps repeated labels (multi-part
episodes, recurring subjects) to one request each.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

API_URL = "https://www.wikidata.org/w/api.php"


@dataclass
class WikidataMatch:
    qid: str
    label: str


class Canonicalizer:
    def __init__(self, client: httpx.Client):
        self.client = client
        self._cache: dict[str, WikidataMatch | None] = {}

    def canonicalize(self, label: str) -> WikidataMatch | None:
        key = label.casefold()
        if key not in self._cache:
            self._cache[key] = self._lookup(label)
        return self._cache[key]

    def _lookup(self, label: str) -> WikidataMatch | None:
        try:
            resp = self.client.get(
                API_URL,
                params={
                    "action": "wbsearchentities",
                    "search": label,
                    "language": "en",
                    "type": "item",
                    "limit": 1,
                    "format": "json",
                },
            )
            resp.raise_for_status()
            hits = resp.json().get("search", [])
        except (httpx.HTTPError, ValueError):
            return None
        if not hits:
            return None
        hit = hits[0]
        qid, hit_label = hit.get("id"), hit.get("label")
        if not qid or not hit_label:
            return None
        return WikidataMatch(qid=qid, label=hit_label)
