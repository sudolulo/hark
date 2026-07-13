"""External show ratings — currently just Podchaser's free-tier GraphQL API
(25,000 query points/month). scoring.py treats this as one input among
several, not the whole story of "interesting" — see that module's docstring.

Cached in its own table (show_ratings, db.py's SCHEMA) rather than fetched
live per page view: App.db() (views.py) only ever holds a read-only
connection, and a rate-limited external API shouldn't be hit on every
request anyway. hark rate-shows (cli.py) refreshes the cache periodically.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx

from .db import utcnow

PODCHASER_GRAPHQL_URL = "https://api.podchaser.com/graphql"
SOURCE = "podchaser"

# Re-attempt a show whose last fetch (hit or miss) is older than this —
# not fetched fresh every run, to respect the free tier's query-point budget.
RATINGS_STALE_DAYS = 30


class PodchaserError(Exception):
    """A GraphQL-level error (bad key, malformed query) — Podchaser can
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
    whenever $HARK_PODCHASER_CLIENT_ID/$HARK_PODCHASER_CLIENT_SECRET aren't
    set (hark rate-shows still runs the itunes_id backfill in that case;
    only the ratings half is skipped)."""

    def fetch(self, feed_url: str, itunes_id: int | None) -> ShowRating | None:
        return None


_ACCESS_TOKEN_MUTATION = """
mutation($input: AccessTokenRequest!) {
  requestAccessToken(input: $input) {
    access_token
    token_type
  }
}
"""

_PODCAST_QUERY = """
query($identifier: PodcastIdentifier!) {
  podcast(identifier: $identifier) {
    id
    ratingAverage
    ratingCount
  }
}
"""


class PodchaserRatingsSource:
    """Queries by RSS feed URL first, falling back to Apple Podcast ID if
    that misses — both are exact-identifier lookups, not fuzzy title search,
    so there's no risk of matching the wrong show (same reasoning
    resolve.backfill_itunes_ids applies to verifying its own matches).

    Auth is OAuth2 client-credentials, not a bare API key: client_id (the
    account's "API Key") and client_secret (its "API secret"), both from the
    API tab of the account's settings page, are exchanged for a Bearer
    access token via a requestAccessToken *mutation* — there's no separate
    REST token endpoint, it's GraphQL end to end. The token is requested
    once per PodchaserRatingsSource instance, lazily on first fetch() (not
    eagerly in __init__, so a batch with nothing to refresh never
    authenticates for no reason) and reused for every query after that —
    Podchaser's own docs state access tokens are valid for about a year, far
    longer than any single hark rate-shows run.

    Query/mutation shapes here are corroborated across several independent
    sources (support articles, schema reference pages, a third-party
    integration example) as of 2026-07, since Podchaser's own interactive
    docs site (api-docs.podchaser.com) blocked direct fetching with a 403
    during development — reasonably confident, but not verified against a
    live schema browser. If fetch() raises PodchaserError for every show,
    check requestAccessToken's exact input/response shape and the
    `PodcastIdentifier` type enum (`RSS`, `APPLE_PODCASTS`) at
    api-docs.podchaser.com first.
    """

    def __init__(self, client: httpx.Client, client_id: str, client_secret: str):
        self.client = client
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token: str | None = None

    def _post(self, query: str, variables: dict, authenticated: bool) -> dict:
        headers = {"Authorization": f"Bearer {self._access_token}"} if authenticated else {}
        resp = self.client.post(
            PODCHASER_GRAPHQL_URL, json={"query": query, "variables": variables}, headers=headers
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise PodchaserError(str(body["errors"]))
        return body.get("data") or {}

    def _authenticate(self) -> str:
        if self._access_token is not None:
            return self._access_token
        data = self._post(
            _ACCESS_TOKEN_MUTATION,
            {"input": {
                "grant_type": "CLIENT_CREDENTIALS",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }},
            authenticated=False,
        )
        token = (data.get("requestAccessToken") or {}).get("access_token")
        if not token:
            raise PodchaserError("requestAccessToken returned no access_token")
        self._access_token = token
        return token

    def _query(self, identifier: dict) -> ShowRating | None:
        self._authenticate()
        data = self._post(_PODCAST_QUERY, {"identifier": identifier}, authenticated=True)
        podcast = data.get("podcast")
        if not podcast:
            return None
        return ShowRating(
            external_id=str(podcast["id"]) if podcast.get("id") is not None else None,
            rating_avg=podcast.get("ratingAverage"),
            rating_count=podcast.get("ratingCount"),
        )

    def fetch(self, feed_url: str, itunes_id: int | None) -> ShowRating | None:
        rating = self._query({"id": feed_url, "type": "RSS"})
        if rating is not None:
            return rating
        if itunes_id is not None:
            return self._query({"id": str(itunes_id), "type": "APPLE_PODCASTS"})
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
    against the query-point budget on every run. Per-show isolated: one
    show's failure (network, or a real PodchaserError) doesn't lose progress
    on the rest of the batch."""
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
            # surface here isn't just network (see PodchaserError above), so
            # a narrower except would miss a real, diagnosable API error.
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
