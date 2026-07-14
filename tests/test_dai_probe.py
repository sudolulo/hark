import httpx
import pytest

from adscrub import dai
from hark import dai_probe, db


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def add_show(conn, query, hosting_platform=None):
    conn.execute(
        "INSERT INTO shows (query, hosting_platform) VALUES (?, ?)", (query, hosting_platform)
    )
    conn.commit()
    return conn.execute("SELECT id FROM shows WHERE query = ?", (query,)).fetchone()["id"]


def add_episode(conn, show_id, guid, audio_url="https://example.com/a.mp3", title=None):
    conn.execute(
        "INSERT INTO episodes (show_id, guid, audio_url, title) VALUES (?, ?, ?, ?)",
        (show_id, guid, audio_url, title or guid),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM episodes WHERE show_id = ? AND guid = ?", (show_id, guid)
    ).fetchone()


# --- select_sample ---


def test_select_sample_picks_up_to_per_platform_per_platform(conn):
    show_a = add_show(conn, "Show A", hosting_platform="acast.com")
    show_b = add_show(conn, "Show B", hosting_platform="megaphone.fm")
    add_episode(conn, show_a, "a1")
    add_episode(conn, show_a, "a2")
    add_episode(conn, show_b, "b1")

    sample = dai_probe.select_sample(conn, per_platform=1)
    platforms = sorted(ep["hosting_platform"] for ep in sample)
    assert platforms == ["acast.com", "megaphone.fm"]  # one per platform, not both from A


def test_select_sample_skips_already_probed_episodes(conn):
    show = add_show(conn, "Show A", hosting_platform="acast.com")
    ep1 = add_episode(conn, show, "a1")
    add_episode(conn, show, "a2")
    conn.execute(
        "INSERT INTO dai_probes (episode_id, platform, tested_at, bytes_compared, diverged)"
        " VALUES (?, 'acast.com', '2026-01-01T00:00:00Z', 100, 0)",
        (ep1["id"],),
    )
    conn.commit()

    sample = dai_probe.select_sample(conn, per_platform=5)
    assert [ep["guid"] for ep in sample] == ["a2"]


def test_select_sample_skips_shows_with_no_platform(conn):
    show = add_show(conn, "Show A", hosting_platform=None)
    add_episode(conn, show, "a1")
    assert dai_probe.select_sample(conn) == []


def test_select_sample_respects_total_limit(conn):
    for i in range(3):
        show = add_show(conn, f"Show {i}", hosting_platform=f"platform-{i}.com")
        add_episode(conn, show, f"g{i}")
    sample = dai_probe.select_sample(conn, per_platform=1, limit=2)
    assert len(sample) == 2


# --- run_probe ---


def client_returning(body_a: bytes, body_b: bytes) -> httpx.Client:
    def handler(request):
        ua = request.headers.get("user-agent", "")
        body = body_a if ua == dai.USER_AGENTS[0] else body_b
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_run_probe_stores_a_diverged_result(conn):
    show = add_show(conn, "Show A", hosting_platform="acast.com")
    ep = add_episode(conn, show, "a1")
    with client_returning(b"same start " * 10 + b"AAA", b"same start " * 10 + b"BBB") as client:
        result = dai_probe.run_probe(client, conn, ep, "acast.com")

    assert result.error is None
    assert result.result.diverged is True

    row = conn.execute("SELECT * FROM dai_probes WHERE episode_id = ?", (ep["id"],)).fetchone()
    assert row["platform"] == "acast.com"
    assert row["diverged"] == 1
    assert row["divergence_byte"] == result.result.divergence_byte


def test_run_probe_stores_a_non_diverged_result(conn):
    show = add_show(conn, "Show A", hosting_platform="acast.com")
    ep = add_episode(conn, show, "a1")
    body = b"identical bytes " * 20
    with client_returning(body, body) as client:
        result = dai_probe.run_probe(client, conn, ep, "acast.com")

    assert result.result.diverged is False
    row = conn.execute("SELECT * FROM dai_probes WHERE episode_id = ?", (ep["id"],)).fetchone()
    assert row["diverged"] == 0
    assert row["divergence_byte"] is None


def test_run_probe_records_an_attempt_on_fetch_failure(conn):
    show = add_show(conn, "Show A", hosting_platform="acast.com")
    ep = add_episode(conn, show, "a1", audio_url="https://example.com/gone.mp3")

    def handler(request):
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = dai_probe.run_probe(client, conn, ep, "acast.com")

    assert result.error is not None
    row = conn.execute("SELECT * FROM dai_probes WHERE episode_id = ?", (ep["id"],)).fetchone()
    assert row is not None  # an attempt was still recorded, so it isn't retried forever
    assert row["bytes_compared"] == 0


# --- platform_summary ---


def test_platform_summary_aggregates_per_platform(conn):
    show = add_show(conn, "Show A", hosting_platform="acast.com")
    ep1 = add_episode(conn, show, "a1")
    ep2 = add_episode(conn, show, "a2")
    conn.execute(
        "INSERT INTO dai_probes (episode_id, platform, tested_at, bytes_compared, diverged,"
        " reconverged) VALUES (?, 'acast.com', '2026-01-01T00:00:00Z', 100, 1, 1)",
        (ep1["id"],),
    )
    conn.execute(
        "INSERT INTO dai_probes (episode_id, platform, tested_at, bytes_compared, diverged)"
        " VALUES (?, 'acast.com', '2026-01-01T00:00:00Z', 100, 0)",
        (ep2["id"],),
    )
    conn.commit()

    rows = dai_probe.platform_summary(conn)
    assert len(rows) == 1
    assert rows[0]["platform"] == "acast.com"
    assert rows[0]["tested"] == 2
    assert rows[0]["diverged"] == 1
    assert rows[0]["reconverged"] == 1
