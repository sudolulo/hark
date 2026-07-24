import json

import pytest

from hark import db, transcript_search


def _write(tmp_path, name, segments):
    p = tmp_path / name
    p.write_text(json.dumps([{"text": t} for t in segments]))
    return str(p)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    if not transcript_search.fts5_available(c):
        pytest.skip("SQLite build has no FTS5")
    c.execute("INSERT INTO shows (query, title) VALUES ('q', 'Show A')")
    c.execute("INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1,'g1','Ep One',?)",
              (_write(tmp_path, "1.json", ["The Dyatlov Pass mystery", "in cold weather"]),))
    c.execute("INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1,'g2','Ep Two',?)",
              (_write(tmp_path, "2.json", ["a completely different story about ships"]),))
    c.commit()
    return c


def test_index_drains_the_queue(conn):
    assert set(transcript_search.pending_index_ids(conn)) == {1, 2}
    indexed, pending = transcript_search.index_transcripts(conn)
    assert indexed == 2 and pending == 0
    assert transcript_search.pending_index_ids(conn) == []          # nothing left to index
    indexed2, _ = transcript_search.index_transcripts(conn)         # idempotent — no re-index
    assert indexed2 == 0


def test_search_finds_the_right_episode_with_a_snippet(conn):
    transcript_search.index_transcripts(conn)
    hits = transcript_search.search(conn, "Dyatlov")
    assert [h["id"] for h in hits] == [1]                            # only ep 1 mentions it
    assert "Dyatlov" in hits[0]["snip"]
    assert transcript_search.search(conn, "ships")[0]["id"] == 2
    assert transcript_search.search(conn, "nonexistentword") == []


def test_search_scopes_to_a_genre(conn):
    transcript_search.index_transcripts(conn)
    conn.execute("INSERT INTO topics (id, label) VALUES (1, 'Dyatlov Pass')")
    conn.execute("INSERT INTO topic_genres (topic_id, genre) VALUES (1, 'history')")
    conn.execute("INSERT INTO episode_topics (episode_id, topic_id) VALUES (1, 1)")
    conn.commit()
    # ep 1 matches the phrase AND falls in 'history' — kept under that filter, dropped under another
    assert [h["id"] for h in transcript_search.search(conn, "Dyatlov", genre="history")] == [1]
    assert transcript_search.search(conn, "Dyatlov", genre="disaster") == []
    assert [h["id"] for h in transcript_search.search(conn, "Dyatlov")] == [1]   # no filter, unchanged


def test_search_is_guarded_before_the_index_exists(tmp_path):
    c = db.connect(tmp_path / "empty.db")   # no transcript_fts table ever created
    assert transcript_search.search(c, "anything") == []            # guarded, not a crash


def test_search_tolerates_query_punctuation(conn):
    transcript_search.index_transcripts(conn)
    assert transcript_search.search(conn, 'cold "weather')  == transcript_search.search(conn, 'cold "weather')
    # a stray quote must not raise an FTS syntax error
    assert transcript_search.search(conn, 'weather"') is not None
