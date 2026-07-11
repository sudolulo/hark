import httpx
import pytest

from hark import db, ingest

FEED_URL = "https://feeds.example.com/case-show"


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO shows (query, feed_url) VALUES (?, ?)",
        ("Example Case Show", FEED_URL),
    )
    conn.commit()
    return conn


def feed_client(fixtures, name, status=200):
    content = (fixtures / name).read_bytes() if name else b""

    def handler(request):
        assert str(request.url) == FEED_URL
        return httpx.Response(status, content=content)

    return httpx.Client(transport=httpx.MockTransport(handler))


def show_row(conn):
    return conn.execute("SELECT * FROM shows").fetchone()


# --- parsing ---


def test_parse_feed(fixtures):
    parsed = ingest.parse_feed((fixtures / "feed_a.xml").read_bytes())
    assert parsed.title == "Example Case Show"
    assert parsed.description == "One real-world case per episode."
    assert parsed.image_url == "https://example.com/art.png"
    assert len(parsed.episodes) == 3

    ep1, ep2, ep3 = parsed.episodes
    assert ep1.guid == "ep-001"
    assert ep1.title == "Case 1: The Somerton Man"
    assert ep1.pubdate == "2025-01-01T06:00:00Z"
    assert ep1.duration_seconds == 3723
    assert ep1.audio_url == "https://example.com/audio/ep1.mp3"
    assert ep2.duration_seconds == 2700
    # no <guid> → enclosure URL stands in
    assert ep3.guid == "https://example.com/audio/ep3.mp3"
    assert ep3.duration_seconds == 1800


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("01:02:03", 3723),
        ("45:00", 2700),
        ("1800", 1800),
        ("1800.5", 1800),
        (None, None),
        ("", None),
        ("abc", None),
    ],
)
def test_parse_duration(value, expected):
    assert ingest.parse_duration(value) == expected


# --- upserts ---


def test_ingest_inserts_then_noop(conn, fixtures):
    with feed_client(fixtures, "feed_a.xml") as client:
        first = ingest.ingest_all(conn, client)[0]
        second = ingest.ingest_all(conn, client)[0]

    assert (first.inserted, first.updated, first.total) == (3, 0, 3)
    assert (second.inserted, second.updated, second.total) == (0, 0, 3)
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 3


def test_ingest_updates_changed_episodes(conn, fixtures):
    with feed_client(fixtures, "feed_a.xml") as client:
        ingest.ingest_all(conn, client)
    with feed_client(fixtures, "feed_a_updated.xml") as client:
        result = ingest.ingest_all(conn, client)[0]

    # ep-004 is new, ep-002 changed, ep-001 and ep-003 untouched
    assert (result.inserted, result.updated, result.total) == (1, 1, 4)
    row = conn.execute(
        "SELECT * FROM episodes WHERE guid = 'ep-002'"
    ).fetchone()
    assert row["title"] == "Case 2: Dyatlov Pass (remastered)"
    assert row["duration_seconds"] == 2790
    assert row["updated_at"] is not None
    untouched = conn.execute(
        "SELECT updated_at FROM episodes WHERE guid = 'ep-001'"
    ).fetchone()
    assert untouched["updated_at"] is None


def test_ingest_fills_show_metadata(conn, fixtures):
    with feed_client(fixtures, "feed_a.xml") as client:
        ingest.ingest_all(conn, client)
    show = show_row(conn)
    assert show["title"] == "Example Case Show"
    assert show["description"] == "One real-world case per episode."
    assert show["image_url"] == "https://example.com/art.png"
    assert show["last_fetched_at"] is not None


def test_ingest_http_error_is_reported_not_raised(conn, fixtures):
    with feed_client(fixtures, "feed_a.xml", status=404) as client:
        result = ingest.ingest_all(conn, client)[0]
    assert result.error is not None
    assert (result.inserted, result.updated, result.total) == (0, 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0


def test_ingest_skips_unresolved_shows(conn, fixtures):
    conn.execute("INSERT INTO shows (query) VALUES ('Unresolved Show')")
    conn.commit()
    with feed_client(fixtures, "feed_a.xml") as client:
        results = ingest.ingest_all(conn, client)
    assert [r.query for r in results] == ["Example Case Show"]
