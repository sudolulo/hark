from hark import cli, db
from hark.extract import NullExtractor


def test_stats_on_empty_db(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "shows:    0 (0 resolved)" in out
    assert "episodes: 0" in out


def test_stats_counts_per_show(tmp_path, capsys):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Example Case Show', 'http://x')"
    )
    conn.executemany(
        "INSERT INTO episodes (show_id, guid, pubdate) VALUES (1, ?, ?)",
        [("g1", "2025-01-01T06:00:00Z"), ("g2", "2025-01-08T06:00:00Z")],
    )
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "shows:    1 (1 resolved)" in out
    assert "episodes: 2" in out
    assert "Example Case Show" in out
    assert "latest 2025-01-08" in out


def test_ingest_with_no_shows_fails(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "ingest"])
    assert rc == 1
    assert "hark resolve" in capsys.readouterr().err


def test_null_extractor_extracts_nothing():
    assert NullExtractor().extract("Case 1: The Somerton Man", "desc") == []


def seed_extracted(path):
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Show A', 'http://x')")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('r', 'Show B', 'http://y')")
    conn.executemany(
        "INSERT INTO episodes (show_id, guid, title, pubdate, extracted_at)"
        " VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z')",
        [
            (1, "g1", "The BTK Killer", "2025-01-01T00:00:00Z"),
            (2, "g2", "Dennis Rader: Bind Torture Kill", "2025-02-01T00:00:00Z"),
        ],
    )
    conn.execute(
        "INSERT INTO topics (label, wikidata_id) VALUES ('Dennis Rader', 'Q2295394')"
    )
    conn.execute("INSERT INTO topic_genres (topic_id, genre) VALUES (1, 'true_crime')")
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, confidence, source)"
        " VALUES (?, 1, 0.9, 'test')",
        [(1,), (2,)],
    )
    conn.commit()
    conn.close()


def test_extract_dry_run_reports_pending(tmp_path, capsys):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'S', 'http://x')")
    conn.execute("INSERT INTO episodes (show_id, guid, title) VALUES (1, 'g1', 'ep')")
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "extract", "--dry-run"])
    assert rc == 0
    assert "pending episodes: 1" in capsys.readouterr().out


def test_who_lists_covering_shows(tmp_path, capsys):
    path = tmp_path / "t.db"
    seed_extracted(path)
    rc = cli.main(["--db", str(path), "who", "rader"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Dennis Rader [Q2295394]" in out
    assert "Show A" in out and "Show B" in out


def test_who_by_qid(tmp_path, capsys):
    path = tmp_path / "t.db"
    seed_extracted(path)
    rc = cli.main(["--db", str(path), "who", "Q2295394"])
    assert rc == 0
    assert "Dennis Rader" in capsys.readouterr().out


def test_who_no_match(tmp_path, capsys):
    path = tmp_path / "t.db"
    seed_extracted(path)
    rc = cli.main(["--db", str(path), "who", "zodiac"])
    assert rc == 1
    assert "no topic matching" in capsys.readouterr().err


def test_topics_ranks_by_cross_show_coverage(tmp_path, capsys):
    path = tmp_path / "t.db"
    seed_extracted(path)
    rc = cli.main(["--db", str(path), "topics"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Dennis Rader" in out
    assert "2 shows" in out
    assert "true_crime" in out


def test_topics_empty_db(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "topics"])
    assert rc == 1
    assert "hark extract" in capsys.readouterr().err
