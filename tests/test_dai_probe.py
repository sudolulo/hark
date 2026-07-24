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


def test_select_sample_prioritizes_fewest_probes_but_does_not_exclude(conn):
    """A single probe is not a reliable verdict (acast.com was observed to
    flip from diverged to identical on an otherwise-identical re-test), so an
    episode with fewer than min_trials attempts must stay eligible — the
    least-probed episode just comes first."""
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
    # a2 (0 probes) is prioritized over a1 (1 probe), but a1 is still eligible
    assert [ep["guid"] for ep in sample] == ["a2", "a1"]


def test_select_sample_stops_resampling_once_min_trials_reached(conn):
    show = add_show(conn, "Show A", hosting_platform="acast.com")
    ep1 = add_episode(conn, show, "a1")
    for _ in range(3):
        conn.execute(
            "INSERT INTO dai_probes (episode_id, platform, tested_at, bytes_compared, diverged)"
            " VALUES (?, 'acast.com', '2026-01-01T00:00:00Z', 100, 0)",
            (ep1["id"],),
        )
    conn.commit()

    sample = dai_probe.select_sample(conn, per_platform=5, min_trials=3)
    assert sample == []  # a1 already has 3 of its 3 required trials


def test_select_sample_min_trials_is_configurable(conn):
    show = add_show(conn, "Show A", hosting_platform="acast.com")
    ep1 = add_episode(conn, show, "a1")
    conn.execute(
        "INSERT INTO dai_probes (episode_id, platform, tested_at, bytes_compared, diverged)"
        " VALUES (?, 'acast.com', '2026-01-01T00:00:00Z', 100, 0)",
        (ep1["id"],),
    )
    conn.commit()

    assert dai_probe.select_sample(conn, per_platform=5, min_trials=1) == []
    sample = dai_probe.select_sample(conn, per_platform=5, min_trials=2)
    assert [ep["guid"] for ep in sample] == ["a1"]


def test_select_sample_skips_proven_non_dai_platforms(conn):
    """A platform probed PROVEN_NON_DAI_TRIALS times with zero divergence stops consuming probe
    budget on its new episodes (5b) — but a platform that could still diverge keeps being probed,
    and the skip is opt-out."""
    dead = add_show(conn, "Dead", hosting_platform="libsyn.com")
    for i in range(dai_probe.PROVEN_NON_DAI_TRIALS):
        ep = add_episode(conn, dead, f"d{i}")
        conn.execute(
            "INSERT INTO dai_probes (episode_id, platform, tested_at, bytes_compared, diverged)"
            " VALUES (?, 'libsyn.com', '2026-01-01T00:00:00Z', 100, 0)", (ep["id"],))
    add_episode(conn, dead, "dnew")                       # fresh, un-probed -> would normally qualify
    live = add_show(conn, "Live", hosting_platform="acast.com")
    add_episode(conn, live, "lnew")
    conn.commit()

    platforms = {ep["hosting_platform"] for ep in dai_probe.select_sample(conn, per_platform=5)}
    assert "acast.com" in platforms                       # still-viable platform keeps getting probed
    assert "libsyn.com" not in platforms                  # proven non-DAI: skipped
    optout = {ep["hosting_platform"]
              for ep in dai_probe.select_sample(conn, per_platform=5, skip_proven_non_dai=False)}
    assert "libsyn.com" in optout                          # opt-out restores the old behaviour


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


def client_factory_returning(body_a: bytes, body_b: bytes):
    def handler(request):
        ua = request.headers.get("user-agent", "")
        body = body_a if ua == dai.USER_AGENTS[0] else body_b
        return httpx.Response(200, content=body)

    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def test_run_probe_stores_a_diverged_result(conn):
    show = add_show(conn, "Show A", hosting_platform="acast.com")
    ep = add_episode(conn, show, "a1")
    factory = client_factory_returning(b"same start " * 10 + b"AAA", b"same start " * 10 + b"BBB")
    result = dai_probe.run_probe(factory, conn, ep, "acast.com")

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
    factory = client_factory_returning(body, body)
    result = dai_probe.run_probe(factory, conn, ep, "acast.com")

    assert result.result.diverged is False
    row = conn.execute("SELECT * FROM dai_probes WHERE episode_id = ?", (ep["id"],)).fetchone()
    assert row["diverged"] == 0
    assert row["divergence_byte"] is None


def test_run_probe_records_an_attempt_on_fetch_failure(conn):
    show = add_show(conn, "Show A", hosting_platform="acast.com")
    ep = add_episode(conn, show, "a1", audio_url="https://example.com/gone.mp3")

    def handler(request):
        return httpx.Response(404)

    factory = lambda: httpx.Client(transport=httpx.MockTransport(handler))  # noqa: E731
    result = dai_probe.run_probe(factory, conn, ep, "acast.com")

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
