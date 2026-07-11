import json
from dataclasses import dataclass

import httpx
import pytest

from hark import db, transcribe

AUDIO_URL = "https://example.com/audio/ep1.mp3"


@dataclass
class FakeSegment:
    start: float
    end: float
    text: str


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def audio_client(calls):
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"fake-mp3-bytes")

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- transcribe_episode (model mocked — no real Whisper inference in tests) ---


class FakeModel:
    def __init__(self, segments):
        self._segments = segments

    def transcribe(self, path, vad_filter=True):
        return self._segments, object()


def test_transcribe_episode_writes_transcript_and_updates_row(conn, tmp_path, monkeypatch):
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (1, 'ep-1', 'Ep 1', ?)",
        (AUDIO_URL,),
    )
    conn.commit()
    ep = conn.execute("SELECT * FROM episodes WHERE guid = 'ep-1'").fetchone()

    fake_segments = [FakeSegment(0.0, 5.0, " Welcome to the show "), FakeSegment(5.0, 8.0, "This is an ad")]
    monkeypatch.setattr(transcribe, "load_model", lambda model_size=None: FakeModel(fake_segments))

    calls = []
    with audio_client(calls) as client:
        path = transcribe.transcribe_episode(conn, ep, client, data_dir=tmp_path)

    assert path == tmp_path / "transcripts" / f"{ep['id']}.json"
    stored = json.loads(path.read_text())
    assert stored == [
        {"start": 0.0, "end": 5.0, "text": "Welcome to the show"},
        {"start": 5.0, "end": 8.0, "text": "This is an ad"},
    ]

    row = conn.execute("SELECT transcript_path FROM episodes WHERE id = ?", (ep["id"],)).fetchone()
    assert row["transcript_path"] == str(path)


# --- pending_episodes ---


def test_pending_episodes_excludes_transcribed_and_chapter_covered(conn):
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.executemany(
        "INSERT INTO episodes (show_id, guid, audio_url, transcript_path) VALUES (1, ?, ?, ?)",
        [
            ("no-audio", None, None),
            ("already-transcribed", "http://a/1.mp3", "data/transcripts/2.json"),
            ("needs-transcription", "http://a/3.mp3", None),
            ("chapter-covered", "http://a/4.mp3", None),
        ],
    )
    conn.execute(
        "INSERT INTO ad_segments (episode_id, start_second, end_second, source)"
        " SELECT id, 0, 10, 'chapter' FROM episodes WHERE guid = 'chapter-covered'"
    )
    conn.commit()

    pending = transcribe.pending_episodes(conn)
    assert [ep["guid"] for ep in pending] == ["needs-transcription"]


def test_pending_episodes_respects_limit(conn):
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.executemany(
        "INSERT INTO episodes (show_id, guid, audio_url) VALUES (1, ?, ?)",
        [("a", "http://a/1.mp3"), ("b", "http://a/2.mp3")],
    )
    conn.commit()
    assert len(transcribe.pending_episodes(conn, limit=1)) == 1
    assert len(transcribe.pending_episodes(conn)) == 2
