import sqlite3

import pytest

from hark import db, scoring


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def add_show(conn, feed_url, title=None):
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES (?, ?, ?)",
                 (feed_url, title or feed_url, feed_url))
    conn.commit()
    return conn.execute("SELECT id FROM shows WHERE feed_url = ?", (feed_url,)).fetchone()["id"]


def add_episode(conn, show_id, guid, audio_url=None, title=None):
    conn.execute(
        "INSERT INTO episodes (show_id, guid, audio_url, title) VALUES (?, ?, ?, ?)",
        (show_id, guid, audio_url, title or guid),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM episodes WHERE show_id = ? AND guid = ?", (show_id, guid)
    ).fetchone()["id"]


def add_topic(conn, label, genres=()):
    conn.execute("INSERT INTO topics (label) VALUES (?)", (label,))
    conn.commit()
    topic_id = conn.execute("SELECT id FROM topics WHERE label = ?", (label,)).fetchone()["id"]
    for genre in genres:
        conn.execute("INSERT INTO topic_genres (topic_id, genre) VALUES (?, ?)", (topic_id, genre))
    conn.commit()
    return topic_id


def link(conn, episode_id, topic_id):
    conn.execute(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, ?, 't')",
        (episode_id, topic_id),
    )
    conn.commit()


def play(conn, user_id, podcast_url, episode_url, position, total, guid=None, occurred_at=None):
    conn.execute(
        """
        INSERT INTO listen_actions
            (user_id, podcast_url, episode_url, episode_guid, action, position, total, occurred_at)
        VALUES (?, ?, ?, ?, 'play', ?, ?, ?)
        """,
        (user_id, podcast_url, episode_url, guid, position, total, occurred_at),
    )
    conn.commit()


def download(conn, user_id, podcast_url, episode_url):
    conn.execute(
        "INSERT INTO listen_actions (user_id, podcast_url, episode_url, action) VALUES (?, ?, ?, 'download')",
        (user_id, podcast_url, episode_url),
    )
    conn.commit()


def rate_show(conn, show_id, rating_avg, rating_count, source="taddy"):
    conn.execute(
        "INSERT INTO show_ratings (show_id, source, rating_avg, rating_count, fetched_at)"
        " VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z')",
        (show_id, source, rating_avg, rating_count),
    )
    conn.commit()


# --- _completion_ratios ---


def test_completion_ratios_uses_max_across_multiple_play_rows(conn):
    show_id = add_show(conn, "https://feeds.example.com/a")
    ep_id = add_episode(conn, show_id, "ep-1")
    play(conn, 1, "https://feeds.example.com/a", "", 100, 1000, guid="ep-1", occurred_at="2026-01-01T00:00:00")
    play(conn, 1, "https://feeds.example.com/a", "", 900, 1000, guid="ep-1", occurred_at="2026-01-02T00:00:00")
    ratios = scoring._completion_ratios(conn, 1)
    assert ratios == {ep_id: 0.9}


def test_completion_ratios_clamps_to_one(conn):
    show_id = add_show(conn, "https://feeds.example.com/a")
    ep_id = add_episode(conn, show_id, "ep-1")
    play(conn, 1, "https://feeds.example.com/a", "", 1500, 1000, guid="ep-1")
    assert scoring._completion_ratios(conn, 1) == {ep_id: 1.0}


def test_completion_ratios_guards_zero_and_null_total(conn):
    show_id = add_show(conn, "https://feeds.example.com/a")
    add_episode(conn, show_id, "ep-1")
    add_episode(conn, show_id, "ep-2")
    play(conn, 1, "https://feeds.example.com/a", "", 100, 0, guid="ep-1")
    play(conn, 1, "https://feeds.example.com/a", "", 100, None, guid="ep-2")
    assert scoring._completion_ratios(conn, 1) == {}


def test_completion_ratios_ignores_download_only_episodes(conn):
    show_id = add_show(conn, "https://feeds.example.com/a")
    add_episode(conn, show_id, "ep-1", audio_url="https://a/ep1.mp3")
    download(conn, 1, "https://feeds.example.com/a", "https://a/ep1.mp3")
    assert scoring._completion_ratios(conn, 1) == {}


def test_completion_ratios_matches_via_guid_scoped_by_show(conn):
    # Two different shows both happen to use the guid "ep-1" — a listen for
    # show A's episode must not be attributed to show B's episode with the
    # same (globally non-unique) guid.
    show_a = add_show(conn, "https://feeds.example.com/a")
    show_b = add_show(conn, "https://feeds.example.com/b")
    ep_a = add_episode(conn, show_a, "ep-1")
    add_episode(conn, show_b, "ep-1")
    play(conn, 1, "https://feeds.example.com/a", "", 500, 1000, guid="ep-1")
    assert scoring._completion_ratios(conn, 1) == {ep_a: 0.5}


def test_completion_ratios_falls_back_to_audio_url_when_guid_missing(conn):
    show_id = add_show(conn, "https://feeds.example.com/a")
    ep_id = add_episode(conn, show_id, "ep-1", audio_url="https://a/ep1.mp3")
    play(conn, 1, "https://feeds.example.com/a", "https://a/ep1.mp3", 500, 1000, guid=None)
    assert scoring._completion_ratios(conn, 1) == {ep_id: 0.5}


def test_completion_ratios_scoped_per_user(conn):
    show_id = add_show(conn, "https://feeds.example.com/a")
    ep_id = add_episode(conn, show_id, "ep-1")
    play(conn, 1, "https://feeds.example.com/a", "", 900, 1000, guid="ep-1")
    play(conn, 2, "https://feeds.example.com/a", "", 100, 1000, guid="ep-1")
    assert scoring._completion_ratios(conn, 1) == {ep_id: 0.9}
    assert scoring._completion_ratios(conn, 2) == {ep_id: 0.1}


# --- _shrink ---


def test_shrink_at_zero_samples_reduces_to_prior():
    assert scoring._shrink({"a": 1.0}, {"a": 0}, prior=0.5, k=5) == {"a": 0.5}


def test_shrink_at_large_n_stays_close_to_raw():
    result = scoring._shrink({"a": 0.9}, {"a": 995}, prior=0.5, k=5)
    assert result["a"] == pytest.approx(0.9, abs=0.01)


def test_shrink_at_n_equal_k_is_an_even_blend():
    result = scoring._shrink({"a": 1.0}, {"a": 5}, prior=0.0, k=5)
    assert result["a"] == pytest.approx(0.5)


# --- genre_affinity / topic_affinity ---


def test_genre_affinity_none_for_zero_listening_history(conn):
    assert scoring.genre_affinity(conn, 1) is None


def test_genre_affinity_shrinks_a_single_play_toward_overall_mean(conn):
    show_id = add_show(conn, "https://feeds.example.com/a")
    true_crime = add_topic(conn, "Case A", genres=("true_crime",))
    history = add_topic(conn, "Case B", genres=("history",))
    ep_a = add_episode(conn, show_id, "ep-a")
    ep_b = add_episode(conn, show_id, "ep-b")
    link(conn, ep_a, true_crime)
    link(conn, ep_b, history)
    # finished the true_crime episode, barely started the history one —
    # overall mean is (1.0 + 0.1) / 2 = 0.55
    play(conn, 1, "https://feeds.example.com/a", "", 1000, 1000, guid="ep-a")
    play(conn, 1, "https://feeds.example.com/a", "", 100, 1000, guid="ep-b")
    affinity = scoring.genre_affinity(conn, 1)
    assert affinity is not None
    # each genre has n=1 play, k=5 -> heavily shrunk toward the 0.55 mean,
    # but still ordered the same way as the raw ratios (true_crime > history)
    assert affinity["true_crime"] > 0.55 > affinity["history"]
    assert affinity["true_crime"] == pytest.approx((1 / 6) * 1.0 + (5 / 6) * 0.55)


def test_topic_affinity_present_only_for_engaged_topics(conn):
    show_id = add_show(conn, "https://feeds.example.com/a")
    engaged = add_topic(conn, "Engaged Case")
    unheard = add_topic(conn, "Unheard Case")
    ep_a = add_episode(conn, show_id, "ep-a")
    add_episode(conn, show_id, "ep-b")
    link(conn, ep_a, engaged)
    play(conn, 1, "https://feeds.example.com/a", "", 900, 1000, guid="ep-a")
    affinity = scoring.topic_affinity(conn, 1)
    assert affinity is not None
    assert engaged in affinity
    assert unheard not in affinity


# --- _weighted_external_ratings ---


def test_weighted_external_ratings_missing_table_returns_empty(tmp_path):
    # A raw connection that never ran db.connect()'s schema setup — same
    # shape as App.db()'s read-only connection hitting a pre-upgrade file.
    raw = sqlite3.connect(tmp_path / "bare.db")
    raw.row_factory = sqlite3.Row
    raw.execute("CREATE TABLE shows (id INTEGER PRIMARY KEY)")
    assert scoring._weighted_external_ratings(raw) == {}


def test_weighted_external_ratings_shrinks_low_review_count_toward_mean(conn):
    show_a = add_show(conn, "https://feeds.example.com/a")
    show_b = add_show(conn, "https://feeds.example.com/b")
    rate_show(conn, show_a, 5.0, 2)      # tiny sample, extreme raw score
    rate_show(conn, show_b, 4.3, 10000)  # huge sample
    weighted = scoring._weighted_external_ratings(conn)
    # the tiny-sample show gets meaningfully pulled down from its raw 5.0...
    assert weighted[show_a] < 5.0
    # ...while the huge-sample show barely moves from its own raw 4.3
    assert weighted[show_b] == pytest.approx(4.3, abs=0.01)


def test_weighted_external_ratings_excludes_unmatched_shows(conn):
    show_id = add_show(conn, "https://feeds.example.com/a")
    rate_show(conn, show_id, None, None)  # a recorded miss, not a rating
    assert scoring._weighted_external_ratings(conn) == {}


# --- recommended_episodes ---


def test_recommended_episodes_collapses_to_external_rating_for_new_user(conn):
    show_a = add_show(conn, "https://feeds.example.com/a")
    show_b = add_show(conn, "https://feeds.example.com/b")
    add_episode(conn, show_a, "ep-a", title="Episode A")
    add_episode(conn, show_b, "ep-b", title="Episode B")
    rate_show(conn, show_a, 4.9, 5000)
    rate_show(conn, show_b, 3.5, 5000)
    ranked = scoring.recommended_episodes(conn, user_id=1)
    assert [r["title"] for r in ranked] == ["Episode A", "Episode B"]
    assert all(r["topic_affinity"] is None and r["genre_affinity"] is None for r in ranked)


def test_recommended_episodes_excludes_already_played_episodes(conn):
    show_id = add_show(conn, "https://feeds.example.com/a")
    add_episode(conn, show_id, "ep-a")
    rate_show(conn, show_id, 4.5, 100)
    play(conn, 1, "https://feeds.example.com/a", "", 900, 1000, guid="ep-a")
    assert scoring.recommended_episodes(conn, user_id=1) == []


def test_recommended_episodes_excludes_episodes_with_no_signal_at_all(conn):
    show_id = add_show(conn, "https://feeds.example.com/a")
    add_episode(conn, show_id, "ep-a")  # no topics, no rating
    assert scoring.recommended_episodes(conn, user_id=1) == []


def test_recommended_episodes_combines_only_present_components(conn):
    # ep-b's show has no rating on file — its score must equal its genre
    # component alone, not be diluted as if a missing rating were a 0.
    listened_show = add_show(conn, "https://feeds.example.com/listened")
    unrated_show = add_show(conn, "https://feeds.example.com/unrated")
    true_crime = add_topic(conn, "Case A", genres=("true_crime",))
    ep_listened = add_episode(conn, listened_show, "ep-listened")
    ep_candidate = add_episode(conn, unrated_show, "ep-b", title="Episode B")
    link(conn, ep_listened, true_crime)
    link(conn, ep_candidate, true_crime)
    play(conn, 1, "https://feeds.example.com/listened", "", 1000, 1000, guid="ep-listened")

    ranked = scoring.recommended_episodes(conn, user_id=1)
    assert len(ranked) == 1
    row = ranked[0]
    assert row["episode_id"] == ep_candidate
    assert row["external_rating"] is None
    assert row["genre_affinity"] is not None
    assert row["score"] == pytest.approx(row["genre_affinity"])


def test_recommended_episodes_respects_limit(conn):
    for i in range(5):
        show_id = add_show(conn, f"https://feeds.example.com/{i}")
        add_episode(conn, show_id, f"ep-{i}")
        rate_show(conn, show_id, 4.0 + i * 0.1, 100)
    ranked = scoring.recommended_episodes(conn, user_id=1, limit=2)
    assert len(ranked) == 2


# --- recommendations_for_user ---


def test_recommendations_for_user_matches_the_standalone_functions(conn):
    show_id = add_show(conn, "https://feeds.example.com/a")
    true_crime = add_topic(conn, "Case A", genres=("true_crime",))
    ep_listened = add_episode(conn, show_id, "ep-a")
    ep_candidate = add_episode(conn, show_id, "ep-b", title="Episode B")
    link(conn, ep_listened, true_crime)
    link(conn, ep_candidate, true_crime)
    play(conn, 1, "https://feeds.example.com/a", "", 900, 1000, guid="ep-a")

    combined = scoring.recommendations_for_user(conn, user_id=1)
    assert combined["genre_affinity"] == scoring.genre_affinity(conn, user_id=1)
    assert combined["topic_affinity"] == scoring.topic_affinity(conn, user_id=1)
    assert combined["recommended"] == scoring.recommended_episodes(conn, user_id=1)
    assert [r["episode_id"] for r in combined["recommended"]] == [ep_candidate]


def test_recommendations_for_user_cold_start(conn):
    combined = scoring.recommendations_for_user(conn, user_id=1)
    assert combined == {"recommended": [], "genre_affinity": None, "topic_affinity": None}
