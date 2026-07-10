"""Canonicalize extracted topic labels against Wikidata.

wbsearchentities is keyless and alias-aware: searching "BTK" and "Dennis
Rader" both land on Q2295394, which is what lets hark merge alias topics
into one row. A per-instance cache keeps repeated labels (multi-part
episodes, recurring subjects) to one request each.
"""

from __future__ import annotations

import email.utils
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

API_URL = "https://www.wikidata.org/w/api.php"
MAX_BACKOFF = 30.0


def _retry_after_seconds(value: str | None, default: float = 5.0) -> float:
    """Parse a Retry-After header: either delta-seconds or an HTTP-date
    (both valid per RFC 7231), capped so a hostile/misconfigured server
    can't stall a bulk run for arbitrarily long."""
    if not value:
        return default
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return default
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(seconds, MAX_BACKOFF))


@dataclass
class WikidataMatch:
    qid: str
    label: str


class Canonicalizer:
    """`delay` seconds between uncached lookups keeps bulk runs under
    Wikimedia's burst limits; 429/5xx responses are retried after backing off
    instead of being swallowed as "no match"."""

    def __init__(self, client: httpx.Client, delay: float = 0.25, retries: int = 2):
        self.client = client
        self.delay = delay
        self.retries = max(0, retries)
        self._cache: dict[str, WikidataMatch | None] = {}

    def canonicalize(self, label: str) -> WikidataMatch | None:
        key = label.casefold()
        if key not in self._cache:
            if self.delay and self._cache:
                time.sleep(self.delay)
            self._cache[key] = self._lookup(label)
        return self._cache[key]

    def _get(self, label: str) -> httpx.Response:
        last_exc: httpx.TransportError | None = None
        for attempt in range(self.retries + 1):
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
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(self.delay or 1.0)
                    continue
                raise
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                time.sleep(_retry_after_seconds(resp.headers.get("retry-after")))
                continue
            return resp
        raise last_exc if last_exc else AssertionError("unreachable: retries >= 0 always iterates")

    def _lookup(self, label: str) -> WikidataMatch | None:
        try:
            resp = self._get(label)
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
