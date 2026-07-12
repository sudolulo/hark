import json

from adscrub import cut as ad_cut
from adscrub import detect as ad_detect
from adscrub import transcribe as ad_transcribe

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


# --- ad-stripping pipeline (calls straight into the adscrub package) ---


def test_chapters_with_nothing_to_scan_fails(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "chapters"])
    assert rc == 1
    assert "no episodes" in capsys.readouterr().err


def test_chapters_skips_disabled_shows(tmp_path, capsys):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query, ad_stripping_enabled) VALUES ('Show A', 0)")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, chapters_url) VALUES"
        " (1, 'g1', 'ep', 'http://a/chapters.json')"
    )
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "chapters"])
    assert rc == 1
    assert "no episodes" in capsys.readouterr().err


def test_transcribe_with_nothing_pending_fails(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "transcribe"])
    assert rc == 1
    assert "no episodes pending" in capsys.readouterr().err


def test_transcribe_dry_run_reports_pending(tmp_path, capsys):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.execute("INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (1, 'g1', 'ep', 'http://a/1.mp3')")
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "transcribe", "--dry-run"])
    assert rc == 0
    assert "pending episodes: 1" in capsys.readouterr().out


def test_transcribe_cross_show_only_scopes_to_cross_show_topics(tmp_path, capsys):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.execute("INSERT INTO shows (query) VALUES ('Show B')")
    conn.execute("INSERT INTO topics (id, label) VALUES (1, 'Cross-show Topic')")
    conn.execute("INSERT INTO topics (id, label) VALUES (2, 'Single-show Topic')")
    # Episode 1 (Show A) and episode 2 (Show B) both cover topic 1 -> cross-show.
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (1, 'g1', 'Ep 1', 'http://a/1.mp3')"
    )
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (2, 'g2', 'Ep 2', 'http://a/2.mp3')"
    )
    # Episode 3 (Show A) covers topic 2 alone -> not cross-show.
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (1, 'g3', 'Ep 3', 'http://a/3.mp3')"
    )
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, ?, 't')",
        [(1, 1), (2, 1), (3, 2)],
    )
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "transcribe", "--dry-run"])
    assert rc == 0
    assert "pending episodes: 3" in capsys.readouterr().out  # unscoped: all 3

    rc = cli.main(["--db", str(path), "transcribe", "--dry-run", "--cross-show-only"])
    assert rc == 0
    assert "pending episodes: 2" in capsys.readouterr().out  # scoped: only episodes 1+2


def test_transcribe_success_path_calls_adscrub_directly(tmp_path, capsys, monkeypatch):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.execute("INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (1, 'g1', 'Ep One', 'http://a/1.mp3')")
    conn.commit()
    conn.close()

    def fake_transcribe_episode(conn, ep, client, model_size=None):
        conn.execute("UPDATE episodes SET transcript_path = 'x.json' WHERE id = ?", (ep["id"],))
        conn.commit()
        return "x.json"

    # patched on the adscrub module itself -- hark.cli's `ad_transcribe` name
    # is the same module object, not a copy, so this is what hark's CLI calls.
    monkeypatch.setattr(ad_transcribe, "transcribe_episode", fake_transcribe_episode)

    rc = cli.main(["--db", str(path), "transcribe"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok    Ep One -> x.json" in out
    assert "transcribed 1 episode(s) (0 failed, 0 still pending)" in out


def test_transcribe_skips_disabled_shows(tmp_path, capsys):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query, ad_stripping_enabled) VALUES ('Show A', 1)")
    conn.execute("INSERT INTO shows (query, ad_stripping_enabled) VALUES ('Show B', 0)")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (1, 'g1', 'Enabled Ep', 'http://a/1.mp3')"
    )
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (2, 'g2', 'Disabled Ep', 'http://a/2.mp3')"
    )
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "transcribe", "--dry-run"])
    assert rc == 0
    assert "pending episodes: 1" in capsys.readouterr().out  # only the enabled show's episode


def test_transcribe_aborts_after_consecutive_failures(tmp_path, capsys, monkeypatch):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    for i in range(7):
        conn.execute(
            "INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (1, ?, ?, ?)",
            (f"g{i}", f"Ep {i}", f"http://a/{i}.mp3"),
        )
    conn.commit()
    conn.close()

    def failing_transcribe_episode(conn, ep, client, model_size=None):
        raise OSError("rate limited")

    monkeypatch.setattr(ad_transcribe, "transcribe_episode", failing_transcribe_episode)

    rc = cli.main(["--db", str(path), "transcribe"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out.count("FAIL") == 5  # aborted after 5 consecutive failures, not all 7
    assert "aborting after 5 consecutive failures" in captured.err
    assert "transcribed 0 episode(s) (5 failed, 7 still pending)" in captured.out


def test_detect_ads_with_nothing_pending_fails(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "detect-ads"])
    assert rc == 1
    assert "no episodes pending" in capsys.readouterr().err


def test_detect_ads_dry_run_reports_pending(tmp_path, capsys):
    path = tmp_path / "t.db"
    transcript_path = tmp_path / "t.json"
    transcript_path.write_text(json.dumps([{"start": 0.0, "end": 1.0, "text": "hi"}]))
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1, 'g1', 'ep', ?)",
        (str(transcript_path),),
    )
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "detect-ads", "--dry-run"])
    assert rc == 0
    assert "pending episodes: 1" in capsys.readouterr().out


def test_detect_ads_success_path_calls_adscrub_directly(tmp_path, capsys, monkeypatch):
    path = tmp_path / "t.db"
    transcript_path = tmp_path / "t.json"
    transcript_path.write_text(json.dumps(
        [{"start": 0.0, "end": 5.0, "text": "a"}, {"start": 5.0, "end": 8.0, "text": "ad"}]
    ))
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1, 'g1', 'Ep One', ?)",
        (str(transcript_path),),
    )
    conn.commit()
    conn.close()

    class FakeMessages:
        def parse(self, **kwargs):
            class Response:
                parsed_output = ad_detect._Detection(
                    ad_spans=[ad_detect._Span(start_segment=1, end_segment=1, reason="ad")]
                )
            return Response()

    class FakeAnthropic:
        def __init__(self):
            self.messages = FakeMessages()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)

    rc = cli.main(["--db", str(path), "detect-ads"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok    Ep One: 1 ad span(s) from transcript" in out
    assert "detected across 1 episode(s) (0 failed, 0 still pending)" in out


def test_detect_ads_aborts_after_consecutive_failures(tmp_path, capsys, monkeypatch):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    # 7 episodes all pointing at a transcript file that doesn't exist, so
    # every detect_episode() call fails before the detector is ever invoked.
    for i in range(7):
        conn.execute(
            "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES"
            " (1, ?, ?, ?)",
            (f"g{i}", f"Ep {i}", str(tmp_path / f"missing_{i}.json")),
        )
    conn.commit()
    conn.close()

    class FakeAnthropic:
        def __init__(self):
            self.messages = None

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)

    rc = cli.main(["--db", str(path), "detect-ads"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out.count("FAIL") == 5  # aborted after 5 consecutive failures, not all 7
    assert "aborting after 5 consecutive failures" in captured.err
    assert "detected across 0 episode(s) (5 failed, 7 still pending)" in captured.out


def test_detect_ads_skips_disabled_shows(tmp_path, capsys):
    path = tmp_path / "t.db"
    transcript_path = tmp_path / "t.json"
    transcript_path.write_text(json.dumps([{"start": 0.0, "end": 1.0, "text": "hi"}]))
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query, ad_stripping_enabled) VALUES ('Show A', 0)")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1, 'g1', 'ep', ?)",
        (str(transcript_path),),
    )
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "detect-ads", "--dry-run"])
    assert rc == 0
    assert "pending episodes: 0" in capsys.readouterr().out


def test_cut_skips_disabled_shows(tmp_path, capsys):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query, ad_stripping_enabled) VALUES ('Show A', 0)")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (1, 'g1', 'ep', 'http://a/1.mp3')"
    )
    conn.execute(
        "INSERT INTO ad_segments (episode_id, start_second, end_second, source) VALUES (1, 0, 5, 'chapter')"
    )
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "cut", "--dry-run"])
    assert rc == 0
    assert "pending episodes: 0" in capsys.readouterr().out


def test_cut_with_nothing_pending_fails(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "cut"])
    assert rc == 1
    assert "no episodes pending cutting" in capsys.readouterr().err


def test_cut_dry_run_reports_pending(tmp_path, capsys):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (1, 'g1', 'ep', 'http://a/1.mp3')"
    )
    conn.execute(
        "INSERT INTO ad_segments (episode_id, start_second, end_second, source) VALUES (1, 0, 5, 'chapter')"
    )
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "cut", "--dry-run"])
    assert rc == 0
    assert "pending episodes: 1" in capsys.readouterr().out


def test_cut_success_path_calls_adscrub_directly(tmp_path, capsys, monkeypatch):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (1, 'g1', 'Ep One', 'http://a/1.mp3')"
    )
    conn.execute(
        "INSERT INTO ad_segments (episode_id, start_second, end_second, source) VALUES (1, 0, 5, 'chapter')"
    )
    conn.commit()
    conn.close()

    def fake_cut_episode(conn, ep, client, data_dir=None):
        conn.execute("UPDATE episodes SET cut_path = 'x.mp3' WHERE id = ?", (ep["id"],))
        conn.commit()
        return "x.mp3", 5.0

    monkeypatch.setattr(ad_cut, "cut_episode", fake_cut_episode)

    rc = cli.main(["--db", str(path), "cut"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok    Ep One: removed 5.0s of ads" in out
    assert "cut 1 episode(s) (0 failed, 0 still pending)" in out
