"""External show ratings — currently just Taddy's free-tier GraphQL API
(taddy.org, 500 requests/month, no credit card). scoring.py treats this as
one input among several, not the whole story of "interesting" — see that
module's docstring.

Taddy has no fine-grained crowd rating (star average + review count) on its
free tier — the signal it does have, `popularityRank`, is a coarse bucket
("TOP_200", "TOP_1000", ...) reflecting where a show sits versus the rest of
Taddy's 4M+-podcast index. hark's mostly-niche true-crime/history catalog
will land outside any tier for most shows, same caveat PLAN.md's M4 section
documents: this is a real but *sparse* signal, not "most shows get rated."

Cached in its own table (show_ratings, db.py's SCHEMA) rather than fetched
live per page view: App.db() (views.py) only ever holds a read-only
connection, and a rate-limited external API shouldn't be hit on every
request anyway. hark rate-shows (cli.py) refreshes the cache periodically.

500 requests/month is not a lot, so refresh_ratings() is deliberately
conservative: RATINGS_STALE_DAYS (90) and the even longer
RATINGS_MISS_STALE_DAYS (180) both mean "don't re-check something unlikely
to have changed," and any show already matched to a known Taddy id gets
batched through fetch_many() (up to MAX_BATCH_SIZE at once) instead of one
request each — the steady-state re-check case, and the one that would
otherwise dominate long-run request consumption once the initial catalog
is matched.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx

from .db import utcnow

TADDY_GRAPHQL_URL = "https://api.taddy.org"
SOURCE = "taddy"

# Conservative on purpose — the free tier is 500 requests/month total, and
# a popularity *tier* (coarse buckets, not a live number) doesn't move
# quickly enough to justify checking it monthly, let alone weekly.
RATINGS_STALE_DAYS = 90
# A confirmed "Taddy doesn't have this show at all" is even less likely to
# change soon than an existing match's tier — waits much longer before
# retrying, separately from RATINGS_STALE_DAYS above.
RATINGS_MISS_STALE_DAYS = 180
# getMultiplePodcastSeries' own documented per-request cap.
MAX_BATCH_SIZE = 25


class TaddyError(Exception):
    """A GraphQL-level error (bad credentials, malformed query) — Taddy can
    return HTTP 200 with an "errors" body instead of an HTTP error status,
    so resp.raise_for_status() alone won't catch this; without a separate
    check, a broken query would silently look like "no show found" for
    every single show instead of surfacing as a real, diagnosable error."""


@dataclass
class ShowRating:
    external_id: str | None
    rating_avg: float | None
    rating_count: int | None


@dataclass
class RatingResult:
    show_id: int
    query: str
    rating: ShowRating | None = None
    error: str | None = None


class RatingsSource(Protocol):
    def fetch(self, feed_url: str, itunes_id: int | None) -> ShowRating | None:
        """None if the show isn't found by either identifier — not the same
        as a raised exception, which means the lookup itself failed."""
        ...

    def fetch_many(self, external_ids: list[str]) -> dict[str, ShowRating]:
        """Batch re-fetch for shows already matched to a known external id
        (at most MAX_BATCH_SIZE at a time — refresh_ratings() chunks larger
        lists). Keyed only by the ids actually found; a requested id absent
        from the result means it's no longer resolvable, same as fetch()
        returning None for it."""
        ...


class NullRatingsSource:
    """Placeholder that finds nothing — used by tests, dry paths, and
    whenever $HARK_TADDY_USER_ID/$HARK_TADDY_API_KEY aren't set (hark
    rate-shows still runs the itunes_id backfill in that case; only the
    ratings half is skipped)."""

    def fetch(self, feed_url: str, itunes_id: int | None) -> ShowRating | None:
        return None

    def fetch_many(self, external_ids: list[str]) -> dict[str, ShowRating]:
        return {}


_PODCAST_SERIES_QUERY = """
query($rssUrl: String, $itunesId: Int) {
  getPodcastSeries(rssUrl: $rssUrl, itunesId: $itunesId) {
    uuid
    popularityRank
  }
}
"""

_MULTIPLE_PODCAST_SERIES_QUERY = """
query($uuids: [ID!]!) {
  getMultiplePodcastSeries(uuids: $uuids) {
    uuid
    popularityRank
  }
}
"""

_TOP_N_PATTERN = re.compile(r"^TOP_(\d+)$")


def _score_from_popularity_rank(rank: str | None) -> float | None:
    """Maps a coarse "TOP_N" tier to a 0-5 score for scoring.py's existing
    Bayesian-shrinkage machinery — smaller N (more exclusive, out of
    Taddy's full index) scores higher, log-scaled since these tiers span
    orders of magnitude, not a linear range. An unrecognized tier shape (the
    exact enum isn't documented anywhere I could verify — see the class
    docstring) is treated as no usable signal rather than guessed at.

    This is a reasonable starting mapping, not calibrated against real
    data — hark rate-shows prints the raw tier per show, so it's easy to
    sanity-check against your own catalog and adjust the constants below."""
    if not rank:
        return None
    match = _TOP_N_PATTERN.match(rank)
    if not match:
        return None
    n = int(match.group(1))
    score = 5.0 - 0.5 * math.log10(max(n, 200) / 200)
    return max(2.5, min(5.0, score))


# Confidence weight for the mapped score, fed into scoring.py's Bayesian
# shrinkage — not a count of anything real (unlike a genuine review count),
# so every Taddy-sourced show gets the same weight: a moderate, deliberately
# picked constant reflecting "this is a coarse tier assignment, not a
# fine-grained crowd rating," not a per-show sample size.
_POPULARITY_RANK_CONFIDENCE = 50


class TaddyRatingsSource:
    """Queries by RSS feed URL first, falling back to Apple Podcast ID if
    that misses — both are exact-identifier lookups via getPodcastSeries,
    not fuzzy title search, so there's no risk of matching the wrong show
    (same reasoning resolve.backfill_itunes_ids applies to verifying its own
    matches).

    Auth is two static headers (X-USER-ID, X-API-KEY) from the account's
    developer dashboard — no OAuth token exchange needed, unlike Podchaser
    (ratings.py's earlier design, abandoned once Podchaser turned out to
    need a paid tier for the rating fields themselves).

    Query shape corroborated across Taddy's own docs pages and a worked
    example as of 2026-07 — meaningfully more confidence than the abandoned
    Podchaser attempt had, since these pages were actually fetchable, but
    still not verified against a real account/live schema browser. If
    fetch() raises TaddyError for every show, check getPodcastSeries'
    exact argument names and the popularityRank field at
    taddy.org/developers first.
    """

    def __init__(self, client: httpx.Client, user_id: str, api_key: str):
        self.client = client
        self.user_id = user_id
        self.api_key = api_key

    def _post(self, query: str, variables: dict) -> dict:
        resp = self.client.post(
            TADDY_GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers={"X-USER-ID": self.user_id, "X-API-KEY": self.api_key},
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise TaddyError(str(body["errors"]))
        return body.get("data") or {}

    @staticmethod
    def _to_rating(series: dict) -> ShowRating:
        # A series can be present (Taddy has the show) even when it's
        # outside every popularity tier — the common case for a niche show
        # — recorded as a real match with no rating data, distinct from
        # "not found," so show_ratings itself stays informative about what
        # Taddy actually knows.
        return ShowRating(
            external_id=str(series["uuid"]) if series.get("uuid") else None,
            rating_avg=_score_from_popularity_rank(series.get("popularityRank")),
            rating_count=_POPULARITY_RANK_CONFIDENCE if series.get("popularityRank") else None,
        )

    def _query(self, variables: dict) -> ShowRating | None:
        data = self._post(_PODCAST_SERIES_QUERY, variables)
        series = data.get("getPodcastSeries")
        if not series:
            return None  # Taddy doesn't have this show at all
        return self._to_rating(series)

    def fetch(self, feed_url: str, itunes_id: int | None) -> ShowRating | None:
        rating = self._query({"rssUrl": feed_url})
        if rating is not None:
            return rating
        if itunes_id is not None:
            return self._query({"itunesId": itunes_id})
        return None

    def fetch_many(self, external_ids: list[str]) -> dict[str, ShowRating]:
        """One request for up to MAX_BATCH_SIZE already-known Taddy uuids —
        the steady-state re-check path (has this show's tier moved?) costs
        a fraction of what re-running fetch() per show would, since a show
        already matched once never needs the rssUrl/itunesId lookup again."""
        if not external_ids:
            return {}
        data = self._post(_MULTIPLE_PODCAST_SERIES_QUERY, {"uuids": external_ids})
        by_uuid = {}
        for series in data.get("getMultiplePodcastSeries") or []:
            if series and series.get("uuid"):
                by_uuid[str(series["uuid"])] = self._to_rating(series)
        return by_uuid


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _store_rating(conn: sqlite3.Connection, show_id: int, rating: ShowRating | None, now: str) -> None:
    conn.execute(
        """
        INSERT INTO show_ratings (show_id, source, external_id, rating_avg, rating_count, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (show_id, source) DO UPDATE SET
            external_id = excluded.external_id, rating_avg = excluded.rating_avg,
            rating_count = excluded.rating_count, fetched_at = excluded.fetched_at
        """,
        (show_id, SOURCE, rating.external_id if rating else None,
         rating.rating_avg if rating else None, rating.rating_count if rating else None, now),
    )
    conn.commit()


def refresh_ratings(
    conn: sqlite3.Connection, source: RatingsSource, limit: int | None = None
) -> list[RatingResult]:
    """Fetch/refresh show_ratings for shows with no rating row yet, or whose
    last fetch is older than RATINGS_STALE_DAYS (an existing match — the
    tier may have moved) or RATINGS_MISS_STALE_DAYS (a confirmed miss —
    much less likely to change soon, waits longer). A row is written even
    on a miss (external_id/rating_avg/rating_count left NULL, fetched_at
    still set) — same idiom pipeline._store() already uses for a zero-topic
    episode — so a show the source doesn't have isn't re-queried against
    the request budget on every run.

    Shows with an already-known external id get batched through
    fetch_many() (MAX_BATCH_SIZE per request) instead of one fetch() call
    each — the common steady-state case (re-checking an existing match's
    tier), a large reduction in request count versus the identifier lookup
    every new/unmatched show still needs. --limit caps the total shows
    touched this run, applied before the known/unknown split so it means
    the same thing regardless of which path a given show takes.

    Isolated per-show (individual path) or per-batch (known path): one
    failure (network, or a real TaddyError) doesn't lose progress on the
    rest — a failed batch's shows simply keep their old fetched_at and get
    retried, batched again, next run."""
    cutoff = _cutoff(RATINGS_STALE_DAYS)
    miss_cutoff = _cutoff(RATINGS_MISS_STALE_DAYS)
    sql = """
        SELECT s.id, s.query, s.feed_url, s.itunes_id, r.external_id
        FROM shows s
        LEFT JOIN show_ratings r ON r.show_id = s.id AND r.source = ?
        WHERE s.feed_url IS NOT NULL AND (
            r.fetched_at IS NULL
            OR (r.external_id IS NOT NULL AND r.fetched_at < ?)
            OR (r.external_id IS NULL AND r.fetched_at < ?)
        )
        ORDER BY s.id
    """
    params: list = [SOURCE, cutoff, miss_cutoff]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()

    now = utcnow()
    results: list[RatingResult] = []

    known = [r for r in rows if r["external_id"] is not None]
    for batch in _chunked(known, MAX_BATCH_SIZE):
        by_show_id = {row["id"]: RatingResult(show_id=row["id"], query=row["query"]) for row in batch}
        try:
            ratings_by_id = source.fetch_many([row["external_id"] for row in batch])
        except Exception as exc:  # noqa: BLE001 — batch isolation, matches the
            # per-show isolation below; retried whole (re-batched) next run
            # rather than falling back to per-show requests, which would
            # defeat the point of batching in the first place.
            for result in by_show_id.values():
                result.error = str(exc)
            results.extend(by_show_id.values())
            continue
        for row in batch:
            rating = ratings_by_id.get(row["external_id"])
            _store_rating(conn, row["id"], rating, now)
            by_show_id[row["id"]].rating = rating
        results.extend(by_show_id.values())

    for row in rows:
        if row["external_id"] is not None:
            continue  # handled above via fetch_many
        result = RatingResult(show_id=row["id"], query=row["query"])
        try:
            rating = source.fetch(row["feed_url"], row["itunes_id"])
        except Exception as exc:  # noqa: BLE001 — per-show isolation, matches
            # this codebase's other external-API batch commands (claims.py's
            # compare_pending, pipeline.py's extract_pending) — the failure
            # surface here isn't just network (see TaddyError above), so a
            # narrower except would miss a real, diagnosable API error.
            result.error = str(exc)
            results.append(result)
            continue
        _store_rating(conn, row["id"], rating, now)
        result.rating = rating
        results.append(result)
    return results
