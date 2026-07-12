import json

import httpx
import pytest

from hark import db, resolve


@pytest.fixture
def search_payload(fixtures):
    return json.loads((fixtures / "itunes_search.json").read_text())


def make_client(payload):
    def handler(request):
        assert request.url.host == "itunes.apple.com"
        assert request.url.params["media"] == "podcast"
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_read_feeds_file_skips_comments_and_blanks(tmp_path):
    feeds = tmp_path / "feeds.txt"
    feeds.write_text("# genre\nShow One\n\n  Show Two  \n# another comment\n")
    assert resolve.read_feeds_file(feeds) == ["Show One", "Show Two"]


def test_resolve_show_picks_first_result_with_feed(search_payload):
    with make_client(search_payload) as client:
        show = resolve.resolve_show(client, "Casefile True Crime")
    assert show is not None
    assert show.title == "Casefile True Crime"
    assert show.feed_url == "https://feeds.example.com/casefile"
    assert show.itunes_id == 998568017
    assert show.author == "Casefile Presents"


def test_resolve_show_none_when_no_results():
    with make_client({"resultCount": 0, "results": []}) as client:
        assert resolve.resolve_show(client, "nope") is None


def test_resolve_all_upserts_idempotently(tmp_path, search_payload):
    conn = db.connect(tmp_path / "test.db")
    names = ["Casefile True Crime"]
    with make_client(search_payload) as client:
        resolve.resolve_all(conn, client, names)
        resolve.resolve_all(conn, client, names)

    rows = conn.execute("SELECT * FROM shows").fetchall()
    assert len(rows) == 1
    assert rows[0]["query"] == "Casefile True Crime"
    assert rows[0]["feed_url"] == "https://feeds.example.com/casefile"


def test_resolve_all_updates_changed_feed_url(tmp_path, search_payload):
    conn = db.connect(tmp_path / "test.db")
    with make_client(search_payload) as client:
        resolve.resolve_all(conn, client, ["Casefile True Crime"])

    moved = json.loads(json.dumps(search_payload))
    moved["results"][1]["feedUrl"] = "https://feeds.example.com/casefile-v2"
    with make_client(moved) as client:
        resolve.resolve_all(conn, client, ["Casefile True Crime"])

    rows = conn.execute("SELECT * FROM shows").fetchall()
    assert len(rows) == 1
    assert rows[0]["feed_url"] == "https://feeds.example.com/casefile-v2"
    assert rows[0]["updated_at"] is not None


def test_resolve_all_reports_misses_without_storing(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    with make_client({"resultCount": 0, "results": []}) as client:
        results = resolve.resolve_all(conn, client, ["Unknown Show"])
    assert results == [("Unknown Show", None)]
    assert conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0] == 0


def test_add_show_by_feed_url_inserts_with_feed_url_as_query(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    added = resolve.add_show_by_feed_url(conn, "https://feeds.example.com/new-show", title="New Show")
    assert added is True
    row = conn.execute("SELECT * FROM shows").fetchone()
    assert row["query"] == "https://feeds.example.com/new-show"
    assert row["feed_url"] == "https://feeds.example.com/new-show"
    assert row["title"] == "New Show"


def test_add_show_by_feed_url_starts_topic_index_disabled(tmp_path):
    # Unreviewed shows (gpodder sync, OPML import, discover --add) shouldn't
    # burn extraction effort until the owner confirms genre relevance from
    # the show page — unlike resolve_show()'s hand-curated path, which
    # keeps the schema default of enabled.
    conn = db.connect(tmp_path / "test.db")
    resolve.add_show_by_feed_url(conn, "https://feeds.example.com/new-show")
    row = conn.execute("SELECT topic_index_enabled FROM shows").fetchone()
    assert row["topic_index_enabled"] == 0


def test_add_show_by_feed_url_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    resolve.add_show_by_feed_url(conn, "https://feeds.example.com/x")
    added_again = resolve.add_show_by_feed_url(conn, "https://feeds.example.com/x")
    assert added_again is False
    assert conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0] == 1


def test_add_show_by_feed_url_skips_url_already_resolved_by_name(tmp_path, search_payload):
    # A show already added via resolve_show()'s iTunes path (a different
    # `query`) must not be re-added under the feed URL as query — that would
    # violate shows.feed_url's own UNIQUE constraint.
    conn = db.connect(tmp_path / "test.db")
    with make_client(search_payload) as client:
        resolve.resolve_all(conn, client, ["Casefile True Crime"])
    added = resolve.add_show_by_feed_url(conn, "https://feeds.example.com/casefile")
    assert added is False
    assert conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0] == 1
