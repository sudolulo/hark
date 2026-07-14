from hark import db, hosting


def test_classify_platform_extracts_registrable_domain():
    assert hosting.classify_platform("https://sphinx.acast.com/p/x/e/y/media.mp3") == "acast.com"
    assert hosting.classify_platform("https://traffic.megaphone.fm/ABC123.mp3") == "megaphone.fm"
    assert hosting.classify_platform("https://api.substack.com/feed/x.mp3") == "substack.com"


def test_classify_platform_handles_multi_part_suffix():
    assert hosting.classify_platform("https://open.live.bbc.co.uk/x.mp3") == "bbc.co.uk"


def test_classify_platform_bare_domain():
    assert hosting.classify_platform("https://thisamericanlife.org/rss.xml") == "thisamericanlife.org"


def test_classify_platform_unparseable_url_returns_none():
    assert hosting.classify_platform("") is None


def test_backfill_hosting_platform_sets_only_unset_shows(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.execute(
        "INSERT INTO shows (query, hosting_platform) VALUES ('Show B', 'already-set.com')"
    )
    conn.execute(
        "INSERT INTO episodes (show_id, guid, audio_url) VALUES"
        " (1, 'g1', 'https://sphinx.acast.com/p/x/e/1/media.mp3')"
    )
    conn.execute(
        "INSERT INTO episodes (show_id, guid, audio_url) VALUES"
        " (2, 'g2', 'https://traffic.megaphone.fm/should-not-be-used.mp3')"
    )
    conn.commit()

    updated = hosting.backfill_hosting_platform(conn)
    assert updated == 1  # only show A, show B already had a value

    rows = {
        r["id"]: r["hosting_platform"]
        for r in conn.execute("SELECT id, hosting_platform FROM shows")
    }
    assert rows[1] == "acast.com"
    assert rows[2] == "already-set.com"  # untouched


def test_backfill_hosting_platform_skips_shows_with_no_audio_url(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO shows (query) VALUES ('Show A')")
    conn.execute("INSERT INTO episodes (show_id, guid) VALUES (1, 'g1')")
    conn.commit()

    updated = hosting.backfill_hosting_platform(conn)
    assert updated == 0
    row = conn.execute("SELECT hosting_platform FROM shows WHERE id = 1").fetchone()
    assert row["hosting_platform"] is None
