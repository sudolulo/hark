import json

import httpx
import pytest

from hark import db, ratings


def graphql_client(handler):
    def wrapped(request):
        assert str(request.url) == ratings.PODCHASER_GRAPHQL_URL
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        return handler(body["variables"]["identifier"])

    return httpx.Client(transport=httpx.MockTransport(wrapped))


def podcast_response(id=None, rating_average=None, rating_count=None):
    podcast = None
    if id is not None:
        podcast = {"id": id, "ratingAverage": rating_average, "ratingCount": rating_count}
    return httpx.Response(200, json={"data": {"podcast": podcast}})


# --- NullRatingsSource ---


def test_null_ratings_source_finds_nothing():
    assert ratings.NullRatingsSource().fetch("https://feeds.example.com/x", 123) is None


# --- PodchaserRatingsSource ---


def test_fetch_matches_by_feed_url():
    def handler(identifier):
        assert identifier == {"id": "https://feeds.example.com/casefile", "type": "RSS"}
        return podcast_response(id="42", rating_average=4.7, rating_count=1200)

    with graphql_client(handler) as client:
        rating = ratings.PodchaserRatingsSource(client, "test-key").fetch(
            "https://feeds.example.com/casefile", 998568017
        )
    assert rating == ratings.ShowRating(external_id="42", rating_avg=4.7, rating_count=1200)


def test_fetch_falls_back_to_itunes_id_when_feed_url_misses():
    calls = []

    def handler(identifier):
        calls.append(identifier)
        if identifier["type"] == "RSS":
            return podcast_response()  # miss
        return podcast_response(id="42", rating_average=4.2, rating_count=50)

    with graphql_client(handler) as client:
        rating = ratings.PodchaserRatingsSource(client, "test-key").fetch(
            "https://feeds.example.com/moved", 998568017
        )
    assert rating == ratings.ShowRating(external_id="42", rating_avg=4.2, rating_count=50)
    assert [c["type"] for c in calls] == ["RSS", "APPLE_PODCASTS"]


def test_fetch_returns_none_when_no_itunes_id_and_feed_url_misses():
    with graphql_client(lambda identifier: podcast_response()) as client:
        rating = ratings.PodchaserRatingsSource(client, "test-key").fetch(
            "https://feeds.example.com/unknown", None
        )
    assert rating is None


def test_fetch_returns_none_when_both_identifiers_miss():
    with graphql_client(lambda identifier: podcast_response()) as client:
        rating = ratings.PodchaserRatingsSource(client, "test-key").fetch(
            "https://feeds.example.com/unknown", 998568017
        )
    assert rating is None


def test_fetch_raises_podchaser_error_on_graphql_error_body():
    def handler(identifier):
        return httpx.Response(200, json={"errors": [{"message": "bad identifier type"}]})

    with graphql_client(handler) as client:
        with pytest.raises(ratings.PodchaserError):
            ratings.PodchaserRatingsSource(client, "test-key").fetch(
                "https://feeds.example.com/x", None
            )


def test_fetch_raises_on_http_error_status():
    def handler(request):
        return httpx.Response(401, json={"message": "unauthorized"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            ratings.PodchaserRatingsSource(client, "bad-key").fetch(
                "https://feeds.example.com/x", None
            )


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
    source = FakeSource({"https://feeds.example.com/a": ratings.ShowRating("42", 4.5, 900)})
    results = ratings.refresh_ratings(conn, source)
    assert results == [ratings.RatingResult(show_id=show_id, query="https://feeds.example.com/a",
                                            rating=ratings.ShowRating("42", 4.5, 900))]
    row = conn.execute("SELECT * FROM show_ratings WHERE show_id = ?", (show_id,)).fetchone()
    assert (row["external_id"], row["rating_avg"], row["rating_count"]) == ("42", 4.5, 900)
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
        "INSERT INTO show_ratings (show_id, source, fetched_at) VALUES (?, 'podchaser', '2020-01-01T00:00:00Z')",
        (show_id,),
    )
    conn.commit()
    source = FakeSource({"https://feeds.example.com/a": ratings.ShowRating("42", 4.5, 900)})
    results = ratings.refresh_ratings(conn, source)
    assert len(results) == 1
    row = conn.execute("SELECT * FROM show_ratings WHERE show_id = ?", (show_id,)).fetchone()
    assert row["external_id"] == "42"


def test_refresh_ratings_isolates_a_failure_and_keeps_prior_progress(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    ok_id = seed_show(conn, "https://feeds.example.com/a")
    boom_id = seed_show(conn, "https://feeds.example.com/boom")
    source = FakeSource({
        "https://feeds.example.com/a": ratings.ShowRating("42", 4.5, 900),
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
        "https://feeds.example.com/a": ratings.ShowRating("1", 4.0, 10),
        "https://feeds.example.com/b": ratings.ShowRating("2", 4.0, 10),
    })
    results = ratings.refresh_ratings(conn, source, limit=1)
    assert len(results) == 1
