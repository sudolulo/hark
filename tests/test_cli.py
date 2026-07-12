import json

from adscrub import cut as ad_cut
from adscrub import detect as ad_detect
from adscrub import transcribe as ad_transcribe

from hark import cli, db, discover, nextcloud
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


def test_fsck_reports_dangling_transcript_paths_without_fix(tmp_path, capsys):
    path = tmp_path / "t.db"
    real_transcript = tmp_path / "real.json"
    real_transcript.write_text("[]")
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1, 'g1', 'Missing', ?)",
        (str(tmp_path / "gone.json"),),
    )
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1, 'g2', 'Present', ?)",
        (str(real_transcript),),
    )
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "fsck"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "1 of 2 transcript_path pointer(s) reference a missing file" in out

    conn = db.connect(path)
    row = conn.execute("SELECT transcript_path FROM episodes WHERE title = 'Missing'").fetchone()
    assert row["transcript_path"] is not None  # dry-run: not cleared without --fix


def test_fsck_fix_clears_only_dangling_pointers(tmp_path, capsys):
    path = tmp_path / "t.db"
    real_transcript = tmp_path / "real.json"
    real_transcript.write_text("[]")
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1, 'g1', 'Missing', ?)",
        (str(tmp_path / "gone.json"),),
    )
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1, 'g2', 'Present', ?)",
        (str(real_transcript),),
    )
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "fsck", "--fix"])
    assert rc == 0
    assert "cleared 1 dangling transcript_path pointer(s)" in capsys.readouterr().out

    conn = db.connect(path)
    missing = conn.execute("SELECT transcript_path FROM episodes WHERE title = 'Missing'").fetchone()
    present = conn.execute("SELECT transcript_path FROM episodes WHERE title = 'Present'").fetchone()
    assert missing["transcript_path"] is None
    assert present["transcript_path"] == str(real_transcript)


def test_fsck_with_nothing_dangling_succeeds(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "fsck"])
    assert rc == 0
    assert "0 of 0 transcript_path pointer(s) reference a missing file" in capsys.readouterr().out


# --- M3: sync-subscriptions / sync-history / import-opml ---


NEXTCLOUD_ARGS = [
    "--nextcloud-url", "https://nc.example", "--nextcloud-user", "u", "--nextcloud-password", "p",
]


def test_sync_subscriptions_requires_credentials(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "sync-subscriptions"])
    assert rc == 1
    assert "HARK_NEXTCLOUD_URL" in capsys.readouterr().err


def test_sync_subscriptions_adds_new_shows_only(tmp_path, capsys, monkeypatch):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query, feed_url) VALUES ('already known', 'https://a.example/feed')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        nextcloud, "current_subscriptions",
        lambda client, url, auth: ["https://a.example/feed", "https://b.example/feed"],
    )

    rc = cli.main(["--db", str(path), "sync-subscriptions", *NEXTCLOUD_ARGS])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok    https://b.example/feed" in out
    assert "https://a.example/feed" not in out  # already known -> not reported as newly added
    assert "synced 2 subscription(s), 1 new" in out

    rows = db.connect(path).execute("SELECT feed_url FROM shows").fetchall()
    assert {r["feed_url"] for r in rows} == {"https://a.example/feed", "https://b.example/feed"}


def test_sync_subscriptions_reports_http_error(tmp_path, capsys, monkeypatch):
    import httpx

    def boom(client, url, auth):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(nextcloud, "current_subscriptions", boom)
    rc = cli.main(["--db", str(tmp_path / "t.db"), "sync-subscriptions", *NEXTCLOUD_ARGS])
    assert rc == 1
    assert "nextcloud:" in capsys.readouterr().err


def test_sync_history_stores_actions_and_advances_cursor(tmp_path, capsys, monkeypatch):
    path = tmp_path / "t.db"
    db.connect(path).close()

    calls = []

    def fake_fetch(client, url, auth, since=0):
        calls.append(since)
        return (
            [{"podcast": "https://a.example/feed", "episode": "https://a.example/e1.mp3",
              "guid": "g1", "action": "PLAY", "position": 10, "total": 100,
              "timestamp": "2026-01-01T00:00:00+00:00"}],
            42,
        )

    monkeypatch.setattr(nextcloud, "fetch_episode_actions", fake_fetch)
    rc = cli.main(["--db", str(path), "sync-history", *NEXTCLOUD_ARGS])
    assert rc == 0
    assert "synced 1 listen action(s) since last run, 1 new" in capsys.readouterr().out
    assert calls == [0]  # first run: no stored cursor yet

    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM listen_actions").fetchone()[0] == 1
    cursor = conn.execute(
        "SELECT value FROM sync_state WHERE key = 'gpodder_episode_action_since'"
    ).fetchone()
    assert cursor["value"] == "42"

    # second run picks up the stored cursor instead of starting from 0
    cli.main(["--db", str(path), "sync-history", *NEXTCLOUD_ARGS])
    assert calls == [0, 42]


def test_sync_history_deduplicates_reinserted_actions(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    db.connect(path).close()

    def fake_fetch(client, url, auth, since=0):
        return (
            [{"podcast": "https://a.example/feed", "episode": "https://a.example/e1.mp3",
              "guid": "g1", "action": "PLAY", "position": 10, "total": 100,
              "timestamp": "2026-01-01T00:00:00+00:00"}],
            10,
        )

    monkeypatch.setattr(nextcloud, "fetch_episode_actions", fake_fetch)
    cli.main(["--db", str(path), "sync-history", *NEXTCLOUD_ARGS])
    cli.main(["--db", str(path), "sync-history", *NEXTCLOUD_ARGS])
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM listen_actions").fetchone()[0] == 1


def test_import_opml_adds_shows_by_feed_url(tmp_path, capsys):
    opml_path = tmp_path / "feeds.opml"
    opml_path.write_text(
        '<opml><body><outline type="rss" title="Show A" xmlUrl="https://a.example/feed.rss"/>'
        '</body></opml>'
    )
    db_path = tmp_path / "t.db"
    rc = cli.main(["--db", str(db_path), "import-opml", str(opml_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok    Show A" in out
    assert "imported 1 feed(s)" in out
    row = db.connect(db_path).execute("SELECT * FROM shows").fetchone()
    assert row["feed_url"] == "https://a.example/feed.rss"
    assert row["title"] == "Show A"


def test_import_opml_missing_file_fails(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "import-opml", str(tmp_path / "nope.opml")])
    assert rc == 1
    assert "cannot read" in capsys.readouterr().err


def test_import_opml_no_feeds_found_fails(tmp_path, capsys):
    opml_path = tmp_path / "empty.opml"
    opml_path.write_text('<opml><body><outline text="Empty folder"/></body></opml>')
    rc = cli.main(["--db", str(tmp_path / "t.db"), "import-opml", str(opml_path)])
    assert rc == 1
    assert "no <outline" in capsys.readouterr().err


# --- discover (M2 candidate-show pipeline) ---


def _fake_candidates(client, terms, limit_per_term=10):
    return [discover.Candidate("Candidate Show", "https://c.example/feed", "True Crime",
                               "Someone", 50, "true crime")]


def test_discover_report_only_does_not_touch_shows(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(discover, "search_candidates", _fake_candidates)
    path = tmp_path / "t.db"
    rc = cli.main(["--db", str(path), "discover"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Candidate Show" in out
    assert "re-run with --add" in out
    assert db.connect(path).execute("SELECT COUNT(*) FROM shows").fetchone()[0] == 0


def test_discover_add_registers_candidates(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(discover, "search_candidates", _fake_candidates)
    path = tmp_path / "t.db"
    rc = cli.main(["--db", str(path), "discover", "--add"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok" in out and "added 1" in out
    row = db.connect(path).execute("SELECT * FROM shows").fetchone()
    assert row["feed_url"] == "https://c.example/feed"
    assert row["title"] == "Candidate Show"


def test_discover_filters_out_already_known_shows(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(discover, "search_candidates", _fake_candidates)
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO shows (query, feed_url) VALUES ('q', 'https://c.example/feed')")
    conn.commit()
    conn.close()
    rc = cli.main(["--db", str(path), "discover"])
    assert rc == 1
    assert "no new candidates found" in capsys.readouterr().err


def test_discover_genre_restricts_seed_terms(tmp_path, monkeypatch):
    seen_terms = []

    def spy(client, terms, limit_per_term=10):
        seen_terms.append(terms)
        return []

    monkeypatch.setattr(discover, "search_candidates", spy)
    cli.main(["--db", str(tmp_path / "t.db"), "discover", "--genre", "cult"])
    assert seen_terms == [list(discover.SEED_TERMS["cult"])]
