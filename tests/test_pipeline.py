from hark import db, pipeline
from hark.extract import ExtractedTopic
from hark.wikidata import WikidataMatch

QIDS = {
    "btk": WikidataMatch(qid="Q2295394", label="Dennis Rader"),
    "dennis rader": WikidataMatch(qid="Q2295394", label="Dennis Rader"),
    "sinking of the titanic": WikidataMatch(qid="Q25173", label="Sinking of the Titanic"),
}


def canonicalize(label):
    return QIDS.get(label.casefold())


class FakeExtractor:
    """Returns topics keyed by episode title; raises on 'boom' titles."""

    def __init__(self, by_title):
        self.by_title = by_title

    def extract(self, title, description):
        if title.startswith("boom"):
            raise RuntimeError("api down")
        return self.by_title.get(title, [])


def seed(conn, titles):
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Show', 'http://x')")
    conn.executemany(
        "INSERT INTO episodes (show_id, guid, title, pubdate) VALUES (1, ?, ?, '2025-01-01T00:00:00Z')",
        [(f"g{i}", t) for i, t in enumerate(titles)],
    )
    conn.commit()


def test_aliases_merge_into_one_topic(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, ["ep1", "ep2"])
    extractor = FakeExtractor({
        "ep1": [ExtractedTopic(label="BTK", genres=("true_crime",), confidence=0.9)],
        "ep2": [ExtractedTopic(label="Dennis Rader", genres=("biography",), confidence=0.8)],
    })
    results = pipeline.extract_pending(conn, extractor, canonicalize, source="test")
    assert [r.error for r in results] == [None, None]

    topics = conn.execute("SELECT label, wikidata_id FROM topics").fetchall()
    assert len(topics) == 1
    assert topics[0]["label"] == "Dennis Rader"
    assert topics[0]["wikidata_id"] == "Q2295394"
    genres = {r["genre"] for r in conn.execute("SELECT genre FROM topic_genres")}
    assert genres == {"true_crime", "biography"}  # merged across episodes
    links = conn.execute("SELECT COUNT(*) FROM episode_topics").fetchone()[0]
    assert links == 2


def test_zero_topic_episode_marked_extracted(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, ["trailer"])
    pipeline.extract_pending(conn, FakeExtractor({}), canonicalize, source="test")
    row = conn.execute("SELECT extracted_at FROM episodes").fetchone()
    assert row["extracted_at"] is not None
    assert pipeline.pending_episodes(conn) == []


def test_pending_episodes_excludes_topic_index_disabled_shows(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO shows (query, title, feed_url, topic_index_enabled)"
        " VALUES ('q', 'Show', 'http://x', 0)"
    )
    conn.execute("INSERT INTO episodes (show_id, guid, title, pubdate) VALUES (1, 'g1', 'ep1', '2025-01-01T00:00:00Z')")
    conn.commit()
    assert pipeline.pending_episodes(conn) == []


def test_failed_episode_stays_pending(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, ["boom", "ep1"])
    extractor = FakeExtractor({
        "ep1": [ExtractedTopic(label="Sinking of the Titanic", genres=("disaster",))],
    })
    results = pipeline.extract_pending(conn, extractor, canonicalize, source="test")
    by_title = {r.title: r for r in results}
    assert by_title["boom"].error == "api down"
    assert by_title["ep1"].error is None
    pending = pipeline.pending_episodes(conn)
    assert [row["title"] for row in pending] == ["boom"]


def test_aborts_after_consecutive_errors(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, [f"boom{i}" for i in range(10)])
    results = pipeline.extract_pending(
        conn, FakeExtractor({}), canonicalize, source="test", max_consecutive_errors=3
    )
    assert len(results) == 3
    assert all(r.error for r in results)


def test_unmatched_label_kept_without_qid(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, ["ep1"])
    extractor = FakeExtractor({
        "ep1": [ExtractedTopic(label="Some Obscure Case", genres=("mystery",), confidence=0.5)],
    })
    pipeline.extract_pending(conn, extractor, canonicalize, source="test")
    row = conn.execute("SELECT label, wikidata_id FROM topics").fetchone()
    assert row["label"] == "Some Obscure Case"
    assert row["wikidata_id"] is None


def test_later_qid_backfills_topic(tmp_path):
    """A label stored without a QID picks one up when a later match provides it."""
    conn = db.connect(tmp_path / "t.db")
    seed(conn, ["ep1", "ep2"])
    extractor = FakeExtractor({
        "ep1": [ExtractedTopic(label="Dennis Rader", genres=())],
        "ep2": [ExtractedTopic(label="Dennis Rader", genres=())],
    })
    lookups = iter([None, QIDS["dennis rader"]])
    pipeline.extract_pending(conn, extractor, lambda label: next(lookups), source="test")
    topics = conn.execute("SELECT label, wikidata_id FROM topics").fetchall()
    assert len(topics) == 1
    assert topics[0]["wikidata_id"] == "Q2295394"


def test_load_extractions_validates_and_stores(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, ["ep1", "ep2"])
    records = [
        {"episode_id": 1, "topics": [
            {"label": "BTK", "genres": ["true_crime", "bogus"], "confidence": 1.4},
            {"label": "  ", "genres": [], "confidence": 0.5},
        ]},
        {"episode_id": 2, "topics": []},
        {"episode_id": 999, "topics": []},
    ]
    results = pipeline.load_extractions(conn, records, canonicalize, source="sess")
    assert [r.error for r in results] == [None, None, "unknown episode_id 999"]
    assert results[0].labels == ["Dennis Rader"]

    row = conn.execute("SELECT label, wikidata_id FROM topics").fetchone()
    assert (row["label"], row["wikidata_id"]) == ("Dennis Rader", "Q2295394")
    genres = [r["genre"] for r in conn.execute("SELECT genre FROM topic_genres")]
    assert genres == ["true_crime"]  # bogus genre dropped
    conf = conn.execute("SELECT confidence, source FROM episode_topics").fetchone()
    assert (conf["confidence"], conf["source"]) == (1.0, "sess")  # clamped
    assert pipeline.pending_episodes(conn) == []  # both marked, incl. zero-topic

    again = pipeline.load_extractions(conn, records[:1], canonicalize, source="sess")
    assert again[0].error is None
    assert again[0].skipped is True


def test_recanonicalize_upgrades_in_place(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, ["ep1"])
    extractor = FakeExtractor({
        "ep1": [ExtractedTopic(label="Snowtown murders", genres=("true_crime",))],
    })
    pipeline.extract_pending(conn, extractor, lambda label: None, source="test")

    results = pipeline.recanonicalize(
        conn, lambda label: WikidataMatch(qid="Q2862221", label="Snowtown murders")
    )
    assert len(results) == 1 and not results[0].merged
    row = conn.execute("SELECT label, wikidata_id FROM topics").fetchone()
    assert (row["label"], row["wikidata_id"]) == ("Snowtown murders", "Q2862221")


def test_recanonicalize_merges_duplicates(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, ["ep1", "ep2"])
    extractor = FakeExtractor({
        "ep1": [ExtractedTopic(label="BTK", genres=("true_crime",), confidence=0.9)],
        "ep2": [ExtractedTopic(label="Dennis Rader", genres=("biography",), confidence=0.8)],
    })
    # offline first pass: no QIDs, so aliases become two separate topics
    pipeline.extract_pending(conn, extractor, lambda label: None, source="test")
    assert conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 2

    results = pipeline.recanonicalize(conn, canonicalize)
    assert any(r.merged for r in results)
    topics = conn.execute("SELECT label, wikidata_id FROM topics").fetchall()
    assert len(topics) == 1
    assert (topics[0]["label"], topics[0]["wikidata_id"]) == ("Dennis Rader", "Q2295394")
    links = conn.execute(
        "SELECT episode_id, confidence FROM episode_topics ORDER BY episode_id"
    ).fetchall()
    assert [row["episode_id"] for row in links] == [1, 2]
    genres = {r["genre"] for r in conn.execute("SELECT genre FROM topic_genres")}
    assert genres == {"true_crime", "biography"}


def test_recanonicalize_does_not_clobber_unrelated_qid_on_label_collision(tmp_path):
    """Two distinct entities sharing a display label (planet vs. element,
    both called "Mercury") must not get merged just because their labels
    coincide — only a QID match (or an unresolved label match) may merge."""
    conn = db.connect(tmp_path / "t.db")
    seed(conn, ["ep1", "ep2"])
    extractor = FakeExtractor({
        "ep1": [ExtractedTopic(label="the planet", genres=("history",))],
        "ep2": [ExtractedTopic(label="quicksilver metal", genres=("history",))],
    })
    pipeline.extract_pending(conn, extractor, lambda label: None, source="test")

    matches = {
        "the planet": WikidataMatch(qid="Q308", label="Mercury"),
        "quicksilver metal": WikidataMatch(qid="Q925", label="Mercury"),
    }
    pipeline.recanonicalize(conn, lambda label: matches[label.casefold()])

    rows = {r["wikidata_id"]: r["label"] for r in conn.execute("SELECT label, wikidata_id FROM topics")}
    assert set(rows) == {"Q308", "Q925"}  # two rows, neither QID overwritten
    assert list(rows.values()).count("Mercury") == 1  # one keeps the plain label
    assert any(v.startswith("Mercury (Q9") for v in rows.values())  # the other is disambiguated


def test_recanonicalize_skips_unmatched(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, ["ep1"])
    extractor = FakeExtractor({
        "ep1": [ExtractedTopic(label="Some Obscure Case", genres=())],
    })
    pipeline.extract_pending(conn, extractor, lambda label: None, source="test")
    assert pipeline.recanonicalize(conn, lambda label: None) == []
    row = conn.execute("SELECT label, wikidata_id FROM topics").fetchone()
    assert (row["label"], row["wikidata_id"]) == ("Some Obscure Case", None)


def test_rerun_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, ["ep1"])
    extractor = FakeExtractor({
        "ep1": [ExtractedTopic(label="BTK", genres=("true_crime",), confidence=0.9)],
    })
    pipeline.extract_pending(conn, extractor, canonicalize, source="test")
    second = pipeline.extract_pending(conn, extractor, canonicalize, source="test")
    assert second == []
    links = conn.execute("SELECT COUNT(*) FROM episode_topics").fetchone()[0]
    assert links == 1
