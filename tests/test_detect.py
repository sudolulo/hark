import json

import pytest

from hark import db, detect

TRANSCRIPT = [
    {"start": 0.0, "end": 5.0, "text": "Welcome to the show."},
    {"start": 5.0, "end": 12.0, "text": "This episode is brought to you by Acme."},
    {"start": 12.0, "end": 15.0, "text": "Use code SAVE10 at acme.com."},
    {"start": 15.0, "end": 40.0, "text": "Now, back to our story."},
]


class StubMessages:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)

        class Response:
            parsed_output = self.parsed

        return Response()


class StubClient:
    def __init__(self, parsed):
        self.messages = StubMessages(parsed)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def seed_episode(conn, tmp_path, transcript=TRANSCRIPT):
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    transcript_path = tmp_path / "t.json"
    transcript_path.write_text(json.dumps(transcript))
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1, 'ep-1', 'Ep 1', ?)",
        (str(transcript_path),),
    )
    conn.commit()
    return conn.execute("SELECT * FROM episodes WHERE guid = 'ep-1'").fetchone()


# --- ClaudeAdDetector ---


def test_detect_maps_segment_indices_to_timestamps():
    parsed = detect._Detection(
        ad_spans=[detect._Span(start_segment=1, end_segment=2, reason="sponsor read")]
    )
    d = detect.ClaudeAdDetector(StubClient(parsed), model="claude-test")
    spans = d.detect(TRANSCRIPT)
    assert len(spans) == 1
    assert spans[0].start_second == 5.0
    assert spans[0].end_second == 15.0
    assert spans[0].reason == "sponsor read"


def test_detect_drops_out_of_range_indices():
    parsed = detect._Detection(
        ad_spans=[
            detect._Span(start_segment=2, end_segment=1, reason="backwards"),
            detect._Span(start_segment=0, end_segment=99, reason="out of range"),
            detect._Span(start_segment=1, end_segment=1, reason="valid"),
        ]
    )
    d = detect.ClaudeAdDetector(StubClient(parsed))
    spans = d.detect(TRANSCRIPT)
    assert len(spans) == 1
    assert spans[0].reason == "valid"


def test_detect_refusal_returns_empty():
    assert detect.ClaudeAdDetector(StubClient(None)).detect(TRANSCRIPT) == []


def test_detect_empty_transcript_returns_empty_without_calling_model():
    client = StubClient(None)
    assert detect.ClaudeAdDetector(client).detect([]) == []
    assert client.messages.calls == []


def test_detect_request_shape():
    client = StubClient(detect._Detection(ad_spans=[]))
    detect.ClaudeAdDetector(client, model="claude-test").detect(TRANSCRIPT)
    call = client.messages.calls[0]
    assert call["model"] == "claude-test"
    assert call["output_format"] is detect._Detection
    body = call["messages"][0]["content"]
    assert "[1] 5.0-12.0: This episode is brought to you by Acme." in body


# --- pending_episodes / detect_pending / _store ---


def test_pending_episodes_excludes_no_transcript_and_already_detected(conn, tmp_path):
    ep = seed_episode(conn, tmp_path)
    assert [e["id"] for e in detect.pending_episodes(conn)] == [ep["id"]]

    conn.execute(
        "UPDATE episodes SET llm_detected_at = '2026-01-01T00:00:00Z' WHERE id = ?", (ep["id"],)
    )
    conn.commit()
    assert detect.pending_episodes(conn) == []


def test_detect_pending_stores_spans_and_reason(conn, tmp_path):
    ep = seed_episode(conn, tmp_path)
    parsed = detect._Detection(
        ad_spans=[detect._Span(start_segment=1, end_segment=2, reason="sponsor read")]
    )
    detector = detect.ClaudeAdDetector(StubClient(parsed))

    results = detect.detect_pending(conn, detector)
    assert len(results) == 1
    assert results[0].found == 1
    assert results[0].error is None

    rows = conn.execute("SELECT * FROM ad_segments WHERE episode_id = ?", (ep["id"],)).fetchall()
    assert len(rows) == 1
    assert (rows[0]["start_second"], rows[0]["end_second"]) == (5.0, 15.0)
    assert rows[0]["source"] == "llm"
    assert rows[0]["reason"] == "sponsor read"

    detected_at = conn.execute(
        "SELECT llm_detected_at FROM episodes WHERE id = ?", (ep["id"],)
    ).fetchone()[0]
    assert detected_at is not None

    # already detected — not pending anymore
    assert detect.pending_episodes(conn) == []


def test_detect_pending_marks_processed_even_with_zero_spans(conn, tmp_path):
    """Regression: an episode with no ads must still be marked processed, or
    it gets re-sent to the LLM (and re-billed) every run."""
    ep = seed_episode(conn, tmp_path)
    detector = detect.ClaudeAdDetector(StubClient(detect._Detection(ad_spans=[])))

    results = detect.detect_pending(conn, detector)
    assert results[0].found == 0
    assert results[0].error is None
    assert conn.execute("SELECT COUNT(*) FROM ad_segments").fetchone()[0] == 0

    detected_at = conn.execute(
        "SELECT llm_detected_at FROM episodes WHERE id = ?", (ep["id"],)
    ).fetchone()[0]
    assert detected_at is not None
    assert detect.pending_episodes(conn) == []


def test_detect_pending_isolates_per_episode_failures(conn, tmp_path):
    ep1 = seed_episode(conn, tmp_path)
    conn.execute("INSERT INTO shows (query) VALUES ('Show B')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (2, 'ep-2', 'Ep 2', ?)",
        (str(tmp_path / "missing.json"),),  # file doesn't exist -> FileNotFoundError
    )
    conn.commit()

    parsed = detect._Detection(ad_spans=[])
    detector = detect.ClaudeAdDetector(StubClient(parsed))

    results = {r.title: r for r in detect.detect_pending(conn, detector)}
    assert results["Ep 1"].error is None
    assert results["Ep 2"].error is not None

    # the failed episode's transcript_path is still set, so it's still "pending"
    # (no llm ad_segments row was written for it) and will be retried next run
    assert [e["id"] for e in detect.pending_episodes(conn)] == [
        conn.execute("SELECT id FROM episodes WHERE guid = 'ep-2'").fetchone()[0]
    ]
