import subprocess

import httpx
import pytest

from hark import cut, db

AUDIO_URL = "https://example.com/audio/ep1.mp3"


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def seed_episode(conn, ad_spans=()):
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (1, 'ep-1', 'Ep 1', ?)",
        (AUDIO_URL,),
    )
    conn.commit()
    ep = conn.execute("SELECT * FROM episodes WHERE guid = 'ep-1'").fetchone()
    for start, end, source in ad_spans:
        conn.execute(
            "INSERT INTO ad_segments (episode_id, start_second, end_second, source)"
            " VALUES (?, ?, ?, ?)",
            (ep["id"], start, end, source),
        )
    conn.commit()
    return conn.execute("SELECT * FROM episodes WHERE guid = 'ep-1'").fetchone()


# --- compute_keep_spans ---


def test_compute_keep_spans_no_ads():
    assert cut.compute_keep_spans([], 100.0) == [(0.0, 100.0)]


def test_compute_keep_spans_ad_in_middle():
    assert cut.compute_keep_spans([(40.0, 60.0)], 100.0) == [(0.0, 40.0), (60.0, 100.0)]


def test_compute_keep_spans_ad_at_start_and_end():
    assert cut.compute_keep_spans([(0.0, 10.0), (90.0, 100.0)], 100.0) == [(10.0, 90.0)]


def test_compute_keep_spans_merges_overlapping_spans_from_different_sources():
    # a chapter-sourced span and an llm-sourced span covering roughly the same break
    spans = [(40.0, 65.0), (60.0, 70.0)]
    assert cut.compute_keep_spans(spans, 100.0) == [(0.0, 40.0), (70.0, 100.0)]


def test_compute_keep_spans_merges_adjacent_spans():
    assert cut.compute_keep_spans([(10.0, 20.0), (20.0, 30.0)], 100.0) == [
        (0.0, 10.0), (30.0, 100.0)
    ]


def test_compute_keep_spans_clamps_out_of_range_end():
    assert cut.compute_keep_spans([(90.0, 150.0)], 100.0) == [(0.0, 90.0)]


def test_compute_keep_spans_entire_episode_is_ads():
    assert cut.compute_keep_spans([(0.0, 100.0)], 100.0) == []


# --- cut_audio ---


def test_cut_audio_no_ads_copies_file_without_invoking_ffmpeg(tmp_path, monkeypatch):
    def fail_if_called(*a, **k):
        raise AssertionError("ffmpeg should not be invoked when there's nothing to cut")

    monkeypatch.setattr(subprocess, "run", fail_if_called)

    src = tmp_path / "in.mp3"
    src.write_bytes(b"original-audio-bytes")
    dest = tmp_path / "out" / "out.mp3"
    cut.cut_audio(src, [(0.0, 100.0)], dest)
    assert dest.read_bytes() == b"original-audio-bytes"


def test_cut_audio_invokes_ffmpeg_per_segment_then_concat(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        with open(cmd[-1], "wb") as fh:
            fh.write(b"x")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    src = tmp_path / "in.mp3"
    src.write_bytes(b"original")
    dest = tmp_path / "out.mp3"
    cut.cut_audio(src, [(0.0, 40.0), (60.0, 100.0)], dest)

    assert dest.exists()
    assert len(calls) == 3  # 2 segment extractions + 1 concat
    assert calls[0][:2] == ["ffmpeg", "-y"]
    assert "-ss" in calls[0] and "0.0" in calls[0]
    assert "-ss" in calls[1] and "60.0" in calls[1]
    assert calls[2][3] == "concat"


# --- pending_episodes ---


def test_pending_episodes_requires_ad_segments(conn):
    seed_episode(conn)  # no ad spans at all
    assert cut.pending_episodes(conn) == []


def test_pending_episodes_includes_episode_with_ad_spans(conn):
    ep = seed_episode(conn, ad_spans=[(10.0, 20.0, "chapter")])
    assert [e["id"] for e in cut.pending_episodes(conn)] == [ep["id"]]

    conn.execute("UPDATE episodes SET cut_path = '/x.mp3' WHERE id = ?", (ep["id"],))
    conn.commit()
    assert cut.pending_episodes(conn) == []


# --- cut_episode / cut_pending ---


def audio_client():
    def handler(request):
        return httpx.Response(200, content=b"fake-mp3-bytes")

    return httpx.Client(transport=httpx.MockTransport(handler))


def fake_cut_audio(audio_path, keep_spans, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"cut")


def test_cut_episode_updates_row_and_returns_ad_seconds(conn, tmp_path, monkeypatch):
    ep = seed_episode(conn, ad_spans=[(10.0, 20.0, "chapter")])
    monkeypatch.setattr(cut, "probe_duration", lambda path: 100.0)
    monkeypatch.setattr(cut, "cut_audio", fake_cut_audio)

    with audio_client() as client:
        path, ad_seconds = cut.cut_episode(conn, ep, client, data_dir=tmp_path)

    assert ad_seconds == 10.0
    assert path == tmp_path / "cut" / f"{ep['id']}.mp3"
    assert path.read_bytes() == b"cut"

    row = conn.execute("SELECT cut_path FROM episodes WHERE id = ?", (ep["id"],)).fetchone()
    assert row["cut_path"] == str(path)


def test_cut_pending_isolates_per_episode_failures(conn, tmp_path, monkeypatch):
    ep1 = seed_episode(conn, ad_spans=[(10.0, 20.0, "chapter")])
    conn.execute("INSERT INTO shows (query) VALUES ('Show B')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (2, 'ep-2', 'Ep 2', ?)",
        (AUDIO_URL,),
    )
    conn.commit()
    ep2 = conn.execute("SELECT * FROM episodes WHERE guid = 'ep-2'").fetchone()
    conn.execute(
        "INSERT INTO ad_segments (episode_id, start_second, end_second, source) VALUES (?, 0, 5, 'chapter')",
        (ep2["id"],),
    )
    conn.commit()

    def fake_probe_duration(path):
        if "2" in str(path):
            raise RuntimeError("ffprobe failed")
        return 100.0

    monkeypatch.setattr(cut, "probe_duration", fake_probe_duration)
    monkeypatch.setattr(cut, "cut_audio", fake_cut_audio)

    with audio_client() as client:
        results = {r.title: r for r in cut.cut_pending(conn, client, data_dir=tmp_path)}

    assert results["Ep 1"].error is None
    assert results["Ep 2"].error is not None
    assert cut.pending_episodes(conn) == [
        conn.execute("SELECT * FROM episodes WHERE guid = 'ep-2'").fetchone()
    ]
