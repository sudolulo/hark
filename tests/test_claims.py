import json

import pytest

from hark import claims, db


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


def write_transcript(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(json.dumps([{"start": 0.0, "end": 1.0, "text": text}]))
    return str(path)


def seed_topic(conn, tmp_path, shows_and_texts: dict[str, str], topic_id: int = 1,
               label: str = "Some Case"):
    """Insert one topic covered by one episode per (show name -> transcript text)."""
    conn.execute(
        "INSERT OR IGNORE INTO topics (id, label) VALUES (?, ?)", (topic_id, label)
    )
    for i, (show, text) in enumerate(shows_and_texts.items()):
        show_row = conn.execute("SELECT id FROM shows WHERE query = ?", (show,)).fetchone()
        if show_row is None:
            conn.execute("INSERT INTO shows (query, title) VALUES (?, ?)", (show, show))
            show_id = conn.execute("SELECT id FROM shows WHERE query = ?", (show,)).fetchone()[0]
        else:
            show_id = show_row["id"]
        guid = f"{label}-{show}-{i}"
        transcript_path = write_transcript(tmp_path, f"{guid}.json", text)
        conn.execute(
            "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (?, ?, ?, ?)",
            (show_id, guid, f"{label} ({show})", transcript_path),
        )
        episode_id = conn.execute("SELECT id FROM episodes WHERE guid = ?", (guid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, ?, 't')",
            (episode_id, topic_id),
        )
    conn.commit()


# --- ClaudeComparator ---


def test_compare_request_shape():
    client = StubClient(claims._ComparisonPayload(shared=[], unique_by_show={}))
    claims.ClaudeComparator(client, model="claude-test").compare(
        {"Show A": "the bridge collapsed", "Show B": "the bridge fell down"}
    )
    call = client.messages.calls[0]
    assert call["model"] == "claude-test"
    assert call["output_format"] is claims._ComparisonPayload
    body = call["messages"][0]["content"]
    assert "=== Show A ===\nthe bridge collapsed" in body
    assert "=== Show B ===\nthe bridge fell down" in body


def test_compare_fewer_than_two_episodes_returns_empty_without_calling_model():
    client = StubClient(None)
    assert claims.ClaudeComparator(client).compare({"Show A": "text"}) == claims.Comparison()
    assert client.messages.calls == []


def test_compare_refusal_returns_empty():
    c = claims.ClaudeComparator(StubClient(None))
    assert c.compare({"Show A": "x", "Show B": "y"}) == claims.Comparison()


def test_compare_drops_hallucinated_show_names_and_empty_lists():
    parsed = claims._ComparisonPayload(
        shared=["the victim was 34"],
        unique_by_show={
            "Show A": ["mentions a getaway car"],
            "Show B": [],  # empty -> dropped
            "Show C (made up)": ["not a real input show"],  # not in input -> dropped
        },
    )
    c = claims.ClaudeComparator(StubClient(parsed))
    result = c.compare({"Show A": "...", "Show B": "..."})
    assert result.shared == ["the victim was 34"]
    assert result.unique_by_show == {"Show A": ["mentions a getaway car"]}


# --- pending_topics ---


def test_pending_topics_requires_two_distinct_shows(conn, tmp_path):
    seed_topic(conn, tmp_path, {"Show A": "text one"})
    assert claims.pending_topics(conn) == []

    seed_topic(conn, tmp_path, {"Show B": "text two"}, topic_id=1)  # same topic, 2nd show
    pending = claims.pending_topics(conn)
    assert len(pending) == 1
    assert pending[0]["topic_id"] == 1
    assert {e["show"] for e in pending[0]["episodes"]} == {"Show A", "Show B"}


def test_pending_topics_skips_already_compared_same_set(conn, tmp_path):
    seed_topic(conn, tmp_path, {"Show A": "a", "Show B": "b"})
    claims.compare_pending(conn, claims.NullComparator())
    assert claims.pending_topics(conn) == []


def test_pending_topics_refreshes_when_new_episode_added(conn, tmp_path):
    seed_topic(conn, tmp_path, {"Show A": "a", "Show B": "b"})
    claims.compare_pending(conn, claims.NullComparator())
    assert claims.pending_topics(conn) == []

    seed_topic(conn, tmp_path, {"Show C": "c"}, topic_id=1)  # 3rd show joins the topic
    pending = claims.pending_topics(conn)
    assert len(pending) == 1
    assert {e["show"] for e in pending[0]["episodes"]} == {"Show A", "Show B", "Show C"}


# --- compare_pending / get_comparison ---


def test_compare_pending_stores_and_roundtrips(conn, tmp_path):
    seed_topic(conn, tmp_path, {"Show A": "a", "Show B": "b"}, label="Brian Wells")
    parsed = claims._ComparisonPayload(
        shared=["a bomb collar was used"], unique_by_show={"Show A": ["a detail"]}
    )
    results = claims.compare_pending(conn, claims.ClaudeComparator(StubClient(parsed)))
    assert len(results) == 1
    assert results[0].label == "Brian Wells"
    assert results[0].shared_count == 1
    assert results[0].error is None

    stored = claims.get_comparison(conn, topic_id=1)
    assert stored.shared == ["a bomb collar was used"]
    assert stored.unique_by_show == {"Show A": ["a detail"]}


def test_compare_pending_combines_multiple_episodes_from_the_same_show(conn, tmp_path):
    # Two episodes from "Show A" (e.g. a 2-part case) plus one from "Show B" —
    # both Show A transcripts must reach the comparator, not just one.
    conn.execute("INSERT INTO topics (id, label) VALUES (1, 'Two-Parter')")
    conn.execute("INSERT INTO shows (query, title) VALUES ('Show A', 'Show A')")
    conn.execute("INSERT INTO shows (query, title) VALUES ('Show B', 'Show B')")
    conn.commit()
    for guid, show_id, text in [("a1", 1, "part one text"), ("a2", 1, "part two text"),
                                 ("b1", 2, "other show text")]:
        path = write_transcript(tmp_path, f"{guid}.json", text)
        conn.execute(
            "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (?, ?, ?, ?)",
            (show_id, guid, guid, path),
        )
        episode_id = conn.execute("SELECT id FROM episodes WHERE guid = ?", (guid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, 1, 't')",
            (episode_id,),
        )
    conn.commit()

    parsed = claims._ComparisonPayload(shared=[], unique_by_show={})
    comparator = claims.ClaudeComparator(StubClient(parsed))
    claims.compare_pending(conn, comparator)

    call = comparator.client.messages.calls[0]
    body = call["messages"][0]["content"]
    # Both Show A transcripts must be present, not just the last one seen.
    assert "part one text" in body
    assert "part two text" in body
    assert "other show text" in body
    # episode_ids records all three — refreshing the comparison later must
    # actually be possible, not silently skipped as "already compared".
    stored = conn.execute("SELECT episode_ids FROM topic_comparisons WHERE topic_id = 1").fetchone()
    assert len(json.loads(stored["episode_ids"])) == 3


def test_pending_topics_counts_distinct_shows_not_display_names(conn, tmp_path):
    # Two shows sharing a display name must still count as 2 distinct shows —
    # only shows.query is UNIQUE, not shows.title.
    conn.execute("INSERT INTO topics (id, label) VALUES (1, 'Shared Title Case')")
    conn.execute("INSERT INTO shows (query, title) VALUES ('query-a', 'Same Name')")
    conn.execute("INSERT INTO shows (query, title) VALUES ('query-b', 'Same Name')")
    conn.commit()
    for guid, show_id in [("e1", 1), ("e2", 2)]:
        path = write_transcript(tmp_path, f"{guid}.json", "text")
        conn.execute(
            "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (?, ?, ?, ?)",
            (show_id, guid, guid, path),
        )
        episode_id = conn.execute("SELECT id FROM episodes WHERE guid = ?", (guid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, 1, 't')",
            (episode_id,),
        )
    conn.commit()

    pending = claims.pending_topics(conn)
    assert len(pending) == 1
    assert pending[0]["topic_id"] == 1


def test_get_comparison_missing_returns_none(conn):
    claims.ensure_schema(conn)
    assert claims.get_comparison(conn, topic_id=999) is None


# --- load_comparisons ---


def test_load_comparisons_stores_and_roundtrips(conn, tmp_path):
    seed_topic(conn, tmp_path, {"Show A": "a", "Show B": "b"}, label="Brian Wells")
    results = claims.load_comparisons(conn, [
        {"topic_id": 1, "shared": ["a bomb collar was used"],
         "unique_by_show": {"Show A": ["a detail"]}},
    ])
    assert len(results) == 1
    assert results[0].label == "Brian Wells"
    assert results[0].shared_count == 1
    assert results[0].error is None

    stored = claims.get_comparison(conn, topic_id=1)
    assert stored.shared == ["a bomb collar was used"]
    assert stored.unique_by_show == {"Show A": ["a detail"]}

    row = conn.execute("SELECT model FROM topic_comparisons WHERE topic_id = 1").fetchone()
    assert row["model"] == "session"  # default


def test_load_comparisons_unknown_topic_errors_without_aborting(conn, tmp_path):
    seed_topic(conn, tmp_path, {"Show A": "a", "Show B": "b"}, topic_id=1, label="Real Topic")
    results = claims.load_comparisons(conn, [
        {"topic_id": 999, "shared": [], "unique_by_show": {}},
        {"topic_id": 1, "shared": ["x"], "unique_by_show": {}},
    ])
    assert results[0].error == "no such topic 999"
    assert results[1].error is None
    assert claims.get_comparison(conn, topic_id=1).shared == ["x"]


def test_load_comparisons_isolates_malformed_record(conn, tmp_path):
    seed_topic(conn, tmp_path, {"Show A": "a", "Show B": "b"}, topic_id=1, label="Topic One")
    seed_topic(conn, tmp_path, {"Show A": "a", "Show B": "b"}, topic_id=2, label="Topic Two")
    results = claims.load_comparisons(conn, [
        {"topic_id": 1},  # missing "shared"/"unique_by_show" — must not abort the batch
        {"topic_id": 2, "shared": ["y"], "unique_by_show": {}},
    ])
    assert len(results) == 2
    assert results[0].error is not None
    assert results[1].error is None
    assert claims.get_comparison(conn, topic_id=1) is None  # never stored
    assert claims.get_comparison(conn, topic_id=2).shared == ["y"]  # unaffected


def test_load_comparisons_custom_source(conn, tmp_path):
    seed_topic(conn, tmp_path, {"Show A": "a", "Show B": "b"}, topic_id=1)
    claims.load_comparisons(conn, [{"topic_id": 1, "shared": [], "unique_by_show": {}}],
                             model="claude-opus-4-8")
    row = conn.execute("SELECT model FROM topic_comparisons WHERE topic_id = 1").fetchone()
    assert row["model"] == "claude-opus-4-8"


def test_compare_pending_isolates_per_topic_failures(conn, tmp_path):
    seed_topic(conn, tmp_path, {"Show A": "a", "Show B": "b"}, topic_id=1, label="Topic One")
    # topic 2's episode points at a transcript file that doesn't exist -> read failure
    conn.execute("INSERT INTO topics (id, label) VALUES (2, 'Topic Two')")
    conn.execute("INSERT INTO shows (query, title) VALUES ('Show C', 'Show C')")
    conn.execute("INSERT INTO shows (query, title) VALUES ('Show D', 'Show D')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES "
        "(3, 'missing-c', 'Topic Two (C)', ?)",
        (str(tmp_path / "missing.json"),),
    )
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES "
        "(4, 'missing-d', 'Topic Two (D)', ?)",
        (str(tmp_path / "also_missing.json"),),
    )
    ep_c = conn.execute("SELECT id FROM episodes WHERE guid = 'missing-c'").fetchone()[0]
    ep_d = conn.execute("SELECT id FROM episodes WHERE guid = 'missing-d'").fetchone()[0]
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, 2, 't')",
        [(ep_c,), (ep_d,)],
    )
    conn.commit()

    parsed = claims._ComparisonPayload(shared=["fact"], unique_by_show={})
    results = {r.label: r for r in claims.compare_pending(
        conn, claims.ClaudeComparator(StubClient(parsed))
    )}
    assert results["Topic One"].error is None
    assert results["Topic Two"].error is not None
    assert claims.get_comparison(conn, topic_id=1) is not None
    assert claims.get_comparison(conn, topic_id=2) is None
