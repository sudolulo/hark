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
