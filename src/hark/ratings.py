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

# Re-attempt a show whose last fetch (hit or miss) is older than this —
# not fetched fresh every run, to respect the free tier's request budget.
RATINGS_STALE_DAYS = 30


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


class NullRatingsSource:
    """Placeholder that finds nothing — used by tests, dry paths, and
    whenever $HARK_TADDY_USER_ID/$HARK_TADDY_API_KEY aren't set (hark
    rate-shows still runs the itunes_id backfill in that case; only the
    ratings half is skipped)."""

    def fetch(self, feed_url: str, itunes_id: int | None) -> ShowRating | None:
        return None


_PODCAST_SERIES_QUERY = """
query($rssUrl: String, $itunesId: Int) {
  getPodcastSeries(rssUrl: $rssUrl, itunesId: $itunesId) {
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

    def _query(self, variables: dict) -> ShowRating | None:
        resp = self.client.post(
            TADDY_GRAPHQL_URL,
            json={"query": _PODCAST_SERIES_QUERY, "variables": variables},
            headers={"X-USER-ID": self.user_id, "X-API-KEY": self.api_key},
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise TaddyError(str(body["errors"]))
        series = (body.get("data") or {}).get("getPodcastSeries")
        if not series:
            return None  # Taddy doesn't have this show at all
        # Taddy *has* the show even when it's outside every popularity tier
        # (the common case for a niche show) — recorded as a real match
        # with no rating data, distinct from "not found," so show_ratings
        # itself stays informative about what Taddy actually knows.
        return ShowRating(
            external_id=str(series["uuid"]) if series.get("uuid") else None,
            rating_avg=_score_from_popularity_rank(series.get("popularityRank")),
            rating_count=_POPULARITY_RANK_CONFIDENCE if series.get("popularityRank") else None,
        )

    def fetch(self, feed_url: str, itunes_id: int | None) -> ShowRating | None:
        rating = self._query({"rssUrl": feed_url})
        if rating is not None:
            return rating
        if itunes_id is not None:
            return self._query({"itunesId": itunes_id})
        return None


def _stale_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=RATINGS_STALE_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def refresh_ratings(
    conn: sqlite3.Connection, source: RatingsSource, limit: int | None = None
) -> list[RatingResult]:
    """Fetch/refresh show_ratings for shows with no rating row yet, or whose
    last fetch (hit or miss) is older than RATINGS_STALE_DAYS. A row is
    written even on a miss (external_id/rating_avg/rating_count left NULL,
    fetched_at still set) — same idiom pipeline._store() already uses for a
    zero-topic episode — so a show the source doesn't have isn't re-queried
    against the request budget on every run. Per-show isolated: one show's
    failure (network, or a real TaddyError) doesn't lose progress on the
    rest of the batch."""
    cutoff = _stale_cutoff()
    sql = """
        SELECT s.id, s.query, s.feed_url, s.itunes_id
        FROM shows s
        LEFT JOIN show_ratings r ON r.show_id = s.id AND r.source = ?
        WHERE s.feed_url IS NOT NULL AND (r.fetched_at IS NULL OR r.fetched_at < ?)
        ORDER BY s.id
    """
    params: list = [SOURCE, cutoff]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()

    results = []
    now = utcnow()
    for row in rows:
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
        conn.execute(
            """
            INSERT INTO show_ratings (show_id, source, external_id, rating_avg, rating_count, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (show_id, source) DO UPDATE SET
                external_id = excluded.external_id, rating_avg = excluded.rating_avg,
                rating_count = excluded.rating_count, fetched_at = excluded.fetched_at
            """,
            (row["id"], SOURCE, rating.external_id if rating else None,
             rating.rating_avg if rating else None, rating.rating_count if rating else None, now),
        )
        conn.commit()
        result.rating = rating
        results.append(result)
    return results
