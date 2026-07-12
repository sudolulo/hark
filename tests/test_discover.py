import httpx

from hark import db, discover


def make_client(payload_by_term):
    def handler(request):
        term = request.url.params["term"]
        return httpx.Response(200, json=payload_by_term.get(term, {"results": []}))

    return httpx.Client(transport=httpx.MockTransport(handler))


def result(title, feed_url, genre="True Crime", episodes=100, author="Someone"):
    return {
        "collectionName": title, "feedUrl": feed_url, "primaryGenreName": genre,
        "trackCount": episodes, "artistName": author,
    }


def test_search_candidates_dedupes_across_terms_ranked_by_episode_count():
    payload = {
        "true crime": {"results": [
            result("Small Show", "https://a.example/feed", episodes=10),
            result("Big Show", "https://b.example/feed", episodes=900),
        ]},
        "unsolved murder": {"results": [
            result("Big Show", "https://b.example/feed", episodes=900),  # same feed, dedup
            result("Mid Show", "https://c.example/feed", episodes=200),
        ]},
    }
    with make_client(payload) as client:
        candidates = discover.search_candidates(client, ["true crime", "unsolved murder"])
    assert [c.feed_url for c in candidates] == [
        "https://b.example/feed", "https://c.example/feed", "https://a.example/feed",
    ]
    assert len({c.feed_url for c in candidates}) == 3


def test_search_candidates_skips_results_with_no_feed_url():
    payload = {"true crime": {"results": [{"collectionName": "No Feed"}]}}
    with make_client(payload) as client:
        candidates = discover.search_candidates(client, ["true crime"])
    assert candidates == []


def test_filter_known_drops_already_tracked_shows(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    conn.execute("INSERT INTO shows (query, feed_url) VALUES ('q', 'https://a.example/feed')")
    conn.commit()
    candidates = [
        discover.Candidate("A", "https://a.example/feed", "True Crime", "X", 10, "true crime"),
        discover.Candidate("B", "https://b.example/feed", "True Crime", "X", 10, "true crime"),
    ]
    filtered = discover.filter_known(conn, candidates)
    assert [c.feed_url for c in filtered] == ["https://b.example/feed"]


def test_default_terms_cover_every_genre():
    all_terms = {t for ts in discover.SEED_TERMS.values() for t in ts}
    with make_client({t: {"results": []} for t in all_terms}) as client:
        assert discover.search_candidates(client) == []  # doesn't raise on any seed term
