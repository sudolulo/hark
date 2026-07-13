import time

from hark import db, gpodder_server


def test_record_and_replay_subscription_changes(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    ts = gpodder_server.record_subscription_changes(
        conn, 1, add=["https://a.example/feed", "https://b.example/feed"], remove=[]
    )
    add, remove, cursor = gpodder_server.subscription_changes_since(conn, 1, 0)
    assert set(add) == {"https://a.example/feed", "https://b.example/feed"}
    assert remove == []
    assert cursor >= ts


def test_subscription_changes_since_only_returns_later_events(tmp_path):
    # occurred_at has second granularity and record_subscription_changes()
    # always stamps "now" — two calls in the same wall-clock second would
    # make a since= filter based on a real time.time() read between them
    # flaky (occurred_at > cursor could be False for both). Set the second
    # event's timestamp explicitly instead of relying on real-time gaps.
    conn = db.connect(tmp_path / "t.db")
    gpodder_server.record_subscription_changes(conn, 1, add=["https://a.example/feed"], remove=[])
    cursor_after_first = int(time.time())
    conn.execute(
        "INSERT INTO subscription_changes (user_id, feed_url, action, occurred_at)"
        " VALUES (1, ?, 'add', ?)",
        ("https://b.example/feed", cursor_after_first + 10),
    )
    conn.commit()
    add, remove, _ = gpodder_server.subscription_changes_since(conn, 1, cursor_after_first)
    assert add == ["https://b.example/feed"]


def test_subscription_changes_since_scoped_per_user(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    gpodder_server.record_subscription_changes(conn, 1, add=["https://a.example/feed"], remove=[])
    gpodder_server.record_subscription_changes(conn, 2, add=["https://b.example/feed"], remove=[])
    add1, _, _ = gpodder_server.subscription_changes_since(conn, 1, 0)
    add2, _, _ = gpodder_server.subscription_changes_since(conn, 2, 0)
    assert add1 == ["https://a.example/feed"]
    assert add2 == ["https://b.example/feed"]


def test_record_subscription_changes_registers_new_shows_unreviewed(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    gpodder_server.record_subscription_changes(conn, 1, add=["https://a.example/feed"], remove=[])
    row = conn.execute("SELECT topic_index_enabled FROM shows WHERE feed_url = ?",
                       ("https://a.example/feed",)).fetchone()
    assert row["topic_index_enabled"] == 0


def test_record_subscription_changes_never_deletes_on_remove(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    gpodder_server.record_subscription_changes(conn, 1, add=["https://a.example/feed"], remove=[])
    gpodder_server.record_subscription_changes(conn, 1, add=[], remove=["https://a.example/feed"])
    assert conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0] == 1  # still tracked


def test_record_subscription_changes_populates_user_shows(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    gpodder_server.record_subscription_changes(conn, 1, add=["https://a.example/feed"], remove=[])
    show_id = conn.execute("SELECT id FROM shows WHERE feed_url = ?",
                           ("https://a.example/feed",)).fetchone()["id"]
    assert conn.execute(
        "SELECT 1 FROM user_shows WHERE user_id = 1 AND show_id = ?", (show_id,)
    ).fetchone() is not None


def test_record_subscription_changes_remove_only_clears_user_shows_not_the_show(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    gpodder_server.record_subscription_changes(conn, 1, add=["https://a.example/feed"], remove=[])
    gpodder_server.record_subscription_changes(conn, 1, add=[], remove=["https://a.example/feed"])
    show_id = conn.execute("SELECT id FROM shows WHERE feed_url = ?",
                           ("https://a.example/feed",)).fetchone()["id"]
    assert conn.execute(
        "SELECT 1 FROM user_shows WHERE user_id = 1 AND show_id = ?", (show_id,)
    ).fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0] == 1


def test_record_subscription_changes_two_users_share_the_global_show(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    gpodder_server.record_subscription_changes(conn, 1, add=["https://a.example/feed"], remove=[])
    gpodder_server.record_subscription_changes(conn, 2, add=["https://a.example/feed"], remove=[])
    # One global show row, not one per subscriber — this is what keeps
    # transcription/detection from running twice for the same episode.
    assert conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0] == 1
    show_id = conn.execute("SELECT id FROM shows").fetchone()["id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM user_shows WHERE show_id = ?", (show_id,)
    ).fetchone()[0] == 2


def test_record_episode_actions_stores_play_fields(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    inserted = gpodder_server.record_episode_actions(conn, 1, [{
        "podcast": "https://a.example/feed", "episode": "https://a.example/ep1.mp3",
        "action": "play", "guid": "g1", "timestamp": "2026-01-01T00:00:00",
        "started": 5, "position": 100, "total": 3000,
    }])
    assert inserted == 1
    row = conn.execute("SELECT * FROM listen_actions").fetchone()
    assert row["started"] == 5 and row["position"] == 100 and row["total"] == 3000
    assert row["episode_guid"] == "g1"
    assert row["user_id"] == 1


def test_record_episode_actions_skips_malformed_entries(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    inserted = gpodder_server.record_episode_actions(conn, 1, [
        {"podcast": "https://a.example/feed", "action": "play"},  # missing episode
        {"episode": "https://a.example/ep1.mp3", "action": "play"},  # missing podcast
        {"podcast": "https://a.example/feed", "episode": "https://a.example/ep1.mp3",
         "action": "not-a-real-action"},  # invalid action
        {"podcast": "https://a.example/feed", "episode": "https://a.example/ep2.mp3", "action": "new"},
    ])
    assert inserted == 1
    assert conn.execute("SELECT COUNT(*) FROM listen_actions").fetchone()[0] == 1


def test_record_episode_actions_same_episode_different_users_both_stored(tmp_path):
    # This is the correctness case listen_actions' user_id-inclusive UNIQUE
    # constraint exists for: two accounts playing the same episode at the
    # same occurred_at must not collide/dedupe against each other.
    conn = db.connect(tmp_path / "t.db")
    action = {
        "podcast": "https://a.example/feed", "episode": "https://a.example/ep1.mp3",
        "action": "play", "timestamp": "2026-01-01T00:00:00",
        "started": 0, "position": 10, "total": 1000,
    }
    inserted1 = gpodder_server.record_episode_actions(conn, 1, [action])
    inserted2 = gpodder_server.record_episode_actions(conn, 2, [action])
    assert inserted1 == 1
    assert inserted2 == 1
    assert conn.execute("SELECT COUNT(*) FROM listen_actions").fetchone()[0] == 2


def test_episode_actions_since_only_emits_play_trio_when_complete(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    gpodder_server.record_episode_actions(conn, 1, [{
        "podcast": "https://a.example/feed", "episode": "https://a.example/ep1.mp3",
        "action": "play", "timestamp": "2026-01-01T00:00:00",
        # no started/position/total
    }])
    actions, _ = gpodder_server.episode_actions_since(conn, 1, 0)
    assert len(actions) == 1
    assert "started" not in actions[0]
    assert "position" not in actions[0]


def test_episode_actions_since_shapes_match_antennapod_reader(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    gpodder_server.record_episode_actions(conn, 1, [{
        "podcast": "https://a.example/feed", "episode": "https://a.example/ep1.mp3",
        "guid": "g1", "action": "play", "timestamp": "2026-01-01T00:00:00",
        "started": 0, "position": 42, "total": 1000,
    }])
    actions, cursor = gpodder_server.episode_actions_since(conn, 1, 0)
    assert actions == [{
        "podcast": "https://a.example/feed", "episode": "https://a.example/ep1.mp3",
        "action": "play", "guid": "g1", "timestamp": "2026-01-01T00:00:00",
        "started": 0, "position": 42, "total": 1000,
    }]
    assert isinstance(cursor, int)


def test_episode_actions_since_excludes_earlier_actions(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    gpodder_server.record_episode_actions(conn, 1, [{
        "podcast": "https://a.example/feed", "episode": "https://a.example/old.mp3", "action": "new",
        "timestamp": "2020-01-01T00:00:00",
    }])
    cursor = int(time.time())
    gpodder_server.record_episode_actions(conn, 1, [{
        "podcast": "https://a.example/feed", "episode": "https://a.example/new.mp3", "action": "new",
        "timestamp": "2030-01-01T00:00:00",
    }])
    actions, _ = gpodder_server.episode_actions_since(conn, 1, cursor)
    assert [a["episode"] for a in actions] == ["https://a.example/new.mp3"]


def test_episode_actions_since_scoped_per_user(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    gpodder_server.record_episode_actions(conn, 1, [{
        "podcast": "https://a.example/feed", "episode": "https://a.example/ep1.mp3", "action": "new",
    }])
    gpodder_server.record_episode_actions(conn, 2, [{
        "podcast": "https://a.example/feed", "episode": "https://a.example/ep2.mp3", "action": "new",
    }])
    actions1, _ = gpodder_server.episode_actions_since(conn, 1, 0)
    actions2, _ = gpodder_server.episode_actions_since(conn, 2, 0)
    assert [a["episode"] for a in actions1] == ["https://a.example/ep1.mp3"]
    assert [a["episode"] for a in actions2] == ["https://a.example/ep2.mp3"]
