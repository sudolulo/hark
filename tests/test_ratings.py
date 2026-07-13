import json

import httpx
import pytest

from hark import db, ratings


def graphql_client(handler):
    def wrapped(request):
        assert str(request.url) == ratings.TADDY_GRAPHQL_URL
        assert request.headers["x-user-id"] == "test-user-id"
        assert request.headers["x-api-key"] == "test-api-key"
        body = json.loads(request.content)
        return handler(body["variables"])

    return httpx.Client(transport=httpx.MockTransport(wrapped))


def make_source(client):
    return ratings.TaddyRatingsSource(client, "test-user-id", "test-api-key")


def series_response(uuid=None, popularity_rank=None):
    series = None
    if uuid is not None:
        series = {"uuid": uuid, "popularityRank": popularity_rank}
    return httpx.Response(200, json={"data": {"getPodcastSeries": series}})


# --- _score_from_popularity_rank ---


def test_score_from_popularity_rank_none_for_no_rank():
    assert ratings._score_from_popularity_rank(None) is None


def test_score_from_popularity_rank_none_for_unrecognized_shape():
    assert ratings._score_from_popularity_rank("SOMETHING_ELSE") is None


def test_score_from_popularity_rank_smaller_n_scores_higher():
    top_200 = ratings._score_from_popularity_rank("TOP_200")
    top_5000 = ratings._score_from_popularity_rank("TOP_5000")
    top_100000 = ratings._score_from_popularity_rank("TOP_100000")
    assert top_200 > top_5000 > top_100000
    assert 2.5 <= top_100000 and top_200 <= 5.0


# --- NullRatingsSource ---


def test_null_ratings_source_finds_nothing():
    assert ratings.NullRatingsSource().fetch("https://feeds.example.com/x", 123) is None


# --- TaddyRatingsSource ---


def test_fetch_matches_by_feed_url():
    def handler(variables):
        assert variables == {"rssUrl": "https://feeds.example.com/casefile"}
        return series_response(uuid="abc-123", popularity_rank="TOP_1000")

    with graphql_client(handler) as client:
        rating = make_source(client).fetch("https://feeds.example.com/casefile", 998568017)
    assert rating.external_id == "abc-123"
    assert rating.rating_avg == ratings._score_from_popularity_rank("TOP_1000")
    assert rating.rating_count == ratings._POPULARITY_RANK_CONFIDENCE


def test_fetch_falls_back_to_itunes_id_when_feed_url_misses():
    calls = []

    def handler(variables):
        calls.append(variables)
        if variables.get("rssUrl"):
            return series_response()  # miss
        return series_response(uuid="abc-123", popularity_rank="TOP_1000")

    with graphql_client(handler) as client:
        rating = make_source(client).fetch("https://feeds.example.com/moved", 998568017)
    assert rating.external_id == "abc-123"
    assert len(calls) == 2 and calls[1]["itunesId"] == 998568017


def test_fetch_returns_none_when_no_itunes_id_and_feed_url_misses():
    with graphql_client(lambda variables: series_response()) as client:
        rating = make_source(client).fetch("https://feeds.example.com/unknown", None)
    assert rating is None


def test_fetch_returns_none_when_both_identifiers_miss():
    with graphql_client(lambda variables: series_response()) as client:
        rating = make_source(client).fetch("https://feeds.example.com/unknown", 998568017)
    assert rating is None


def test_fetch_records_a_real_match_with_no_rating_when_show_has_no_tier():
    # Distinct from "not found at all" — Taddy knows this show, it's just
    # outside every popularity tier (the common case for a niche show).
    def handler(variables):
        return series_response(uuid="abc-123", popularity_rank=None)

    with graphql_client(handler) as client:
        rating = make_source(client).fetch("https://feeds.example.com/niche", None)
    assert rating.external_id == "abc-123"
    assert rating.rating_avg is None
    assert rating.rating_count is None


def test_fetch_raises_taddy_error_on_graphql_error_body():
    def handler(variables):
        return httpx.Response(200, json={"errors": [{"message": "bad argument"}]})

    with graphql_client(handler) as client:
        with pytest.raises(ratings.TaddyError):
            make_source(client).fetch("https://feeds.example.com/x", None)


def test_fetch_raises_on_http_error_status():
    def handler(request):
        return httpx.Response(401, json={"message": "unauthorized"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            make_source(client).fetch("https://feeds.example.com/x", None)


# --- refresh_ratings ---


class FakeSource:
    """Deterministic RatingsSource for refresh_ratings() tests — keyed by
    feed_url, raises for feed_urls mapped to an Exception instance."""

    def __init__(self, by_feed_url):
        self.by_feed_url = by_feed_url
        self.calls = []

    def fetch(self, feed_url, itunes_id):
        self.calls.append(feed_url)
        result = self.by_feed_url.get(feed_url)
        if isinstance(result, Exception):
            raise result
        return result


def seed_show(conn, feed_url, itunes_id=None):
    conn.execute(
        "INSERT INTO shows (query, feed_url, itunes_id) VALUES (?, ?, ?)",
        (feed_url, feed_url, itunes_id),
    )
    conn.commit()
    return conn.execute("SELECT id FROM shows WHERE feed_url = ?", (feed_url,)).fetchone()["id"]


def test_refresh_ratings_stores_a_match(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    show_id = seed_show(conn, "https://feeds.example.com/a")
    source = FakeSource({"https://feeds.example.com/a": ratings.ShowRating("42", 4.5, 50)})
    results = ratings.refresh_ratings(conn, source)
    assert results == [ratings.RatingResult(show_id=show_id, query="https://feeds.example.com/a",
                                            rating=ratings.ShowRating("42", 4.5, 50))]
    row = conn.execute("SELECT * FROM show_ratings WHERE show_id = ?", (show_id,)).fetchone()
    assert (row["external_id"], row["rating_avg"], row["rating_count"]) == ("42", 4.5, 50)
    assert row["fetched_at"] is not None


def test_refresh_ratings_records_a_miss_so_it_is_not_requeried(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed_show(conn, "https://feeds.example.com/a")
    source = FakeSource({"https://feeds.example.com/a": None})
    ratings.refresh_ratings(conn, source)
    row = conn.execute("SELECT * FROM show_ratings").fetchone()
    assert row["external_id"] is None and row["rating_avg"] is None
    assert row["fetched_at"] is not None

    # a fresh call within the stale window must not re-query this show
    ratings.refresh_ratings(conn, source)
    assert source.calls == ["https://feeds.example.com/a"]


def test_refresh_ratings_reattempts_stale_rows(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    show_id = seed_show(conn, "https://feeds.example.com/a")
    conn.execute(
        "INSERT INTO show_ratings (show_id, source, fetched_at) VALUES (?, 'taddy', '2020-01-01T00:00:00Z')",
        (show_id,),
    )
    conn.commit()
    source = FakeSource({"https://feeds.example.com/a": ratings.ShowRating("42", 4.5, 50)})
    results = ratings.refresh_ratings(conn, source)
    assert len(results) == 1
    row = conn.execute("SELECT * FROM show_ratings WHERE show_id = ?", (show_id,)).fetchone()
    assert row["external_id"] == "42"


def test_refresh_ratings_isolates_a_failure_and_keeps_prior_progress(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    ok_id = seed_show(conn, "https://feeds.example.com/a")
    boom_id = seed_show(conn, "https://feeds.example.com/boom")
    source = FakeSource({
        "https://feeds.example.com/a": ratings.ShowRating("42", 4.5, 50),
        "https://feeds.example.com/boom": httpx.ConnectError("simulated failure"),
    })
    results = ratings.refresh_ratings(conn, source)
    by_id = {r.show_id: r for r in results}
    assert by_id[ok_id].rating is not None and by_id[ok_id].error is None
    assert by_id[boom_id].rating is None and by_id[boom_id].error is not None
    # the ok show's row must have survived the boom show's failure
    assert conn.execute(
        "SELECT COUNT(*) FROM show_ratings WHERE show_id = ?", (ok_id,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM show_ratings WHERE show_id = ?", (boom_id,)
    ).fetchone()[0] == 0


def test_refresh_ratings_respects_limit(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed_show(conn, "https://feeds.example.com/a")
    seed_show(conn, "https://feeds.example.com/b")
    source = FakeSource({
        "https://feeds.example.com/a": ratings.ShowRating("1", 4.0, 50),
        "https://feeds.example.com/b": ratings.ShowRating("2", 4.0, 50),
    })
    results = ratings.refresh_ratings(conn, source, limit=1)
    assert len(results) == 1
