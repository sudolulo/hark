"""Web frontend tests: real HTTP against a server on an ephemeral port.

Password stretching is tuned down via monkeypatching PW_ITERS so the suite
stays fast; the logic under test is identical.
"""

import base64
import contextlib
import http.client
import json
import threading
import urllib.parse

import pytest

from hark import claims, db, web


@pytest.fixture(autouse=True)
def fast_stretch(monkeypatch):
    monkeypatch.setattr(web, "PW_ITERS", 10)


@pytest.fixture
def server(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Show A', 'http://x')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, pubdate, audio_url, extracted_at)"
        " VALUES (1, 'g1', 'Case 1: Somerton', '2025-01-01T00:00:00Z', 'http://a/1.mp3',"
        " '2026-01-01T00:00:00Z')"
    )
    conn.execute("INSERT INTO topics (label, wikidata_id) VALUES ('Somerton Man', 'Q923144')")
    conn.execute("INSERT INTO topic_genres (topic_id, genre) VALUES (1, 'mystery')")
    conn.execute(
        "INSERT INTO episode_topics (episode_id, topic_id, confidence, source) VALUES (1, 1, 0.9, 't')"
    )
    # user_shows' own backfill only covers shows that existed before the
    # table did — this fixture inserts "Show A" on the same connect() call
    # that creates the (still-empty) table, so it needs this explicit row to
    # show up in the admin account's own default "my shows" view.
    conn.execute("INSERT INTO user_shows (user_id, show_id) VALUES (1, 1)")
    conn.commit()
    conn.close()

    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="letmein")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()


def request(srv, method, path, body=None, cookie=None, auth=None, json_body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(json_body)
    elif body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode(body)
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp, data


def login(srv, password="letmein"):
    resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": password})
    assert resp.status == 303
    set_cookie = resp.getheader("Set-Cookie")
    return set_cookie.split(";")[0]


def test_everything_gated_except_allowlist(server):
    for path in ("/", "/topics", "/topic/1", "/notable", "/search", "/shows", "/show/1",
                 "/episode/1", "/account"):
        resp, _ = request(server, "GET", path)
        assert resp.status == 303, path
        assert resp.getheader("Location") == "/login"
    for path in ("/healthz", "/login", "/static/style.css"):
        resp, _ = request(server, "GET", path)
        assert resp.status == 200, path


def test_security_headers_on_every_response(server):
    for path in ("/healthz", "/login"):
        resp, _ = request(server, "GET", path)
        assert "default-src 'self'" in resp.getheader("Content-Security-Policy")
        assert resp.getheader("X-Content-Type-Options") == "nosniff"
        assert resp.getheader("X-Frame-Options") == "DENY"
        assert resp.getheader("Referrer-Policy") == "same-origin"


def test_no_inline_styles(server):
    # The CSP has no style-src 'unsafe-inline', so any style="..." attribute
    # is silently no-op'd by the browser rather than erroring — easy to miss
    # without actually rendering the page. Guard against it creeping back in.
    cookie = login(server)
    for path in ("/login", "/", "/topics", "/topic/1", "/notable", "/shows", "/show/1",
                 "/search", "/account"):
        _, body = request(server, "GET", path, cookie=cookie)
        assert 'style="' not in body, path


def test_login_with_admin_token_and_browse(server):
    cookie = login(server)
    assert cookie.startswith("hark_session=")

    resp, body = request(server, "GET", "/", cookie=cookie)
    assert resp.status == 200 and "Who covered it?" in body
    assert "Fully indexed" in body  # fixture's one episode is already extracted
    assert 'href="/topics?genre=mystery">mystery (1)' in body  # genre breakdown
    assert "Recently indexed" in body and "Case 1: Somerton" in body
    assert "view all" not in body  # only 1 topic total, nothing more to link to

    resp, body = request(server, "GET", "/topic/1", cookie=cookie)
    assert resp.status == 200
    assert "Somerton Man" in body and "Q923144" in body and "Show A" in body
    assert "href='/show/1'>Show A</a>" in body  # covered-by show pill

    resp, body = request(server, "GET", "/show/1", cookie=cookie)
    assert resp.status == 200
    assert "Show A" in body and "Case 1: Somerton" in body
    assert 'href="/topic/1">Somerton Man</a>' in body  # per-episode topic pill

    resp, _ = request(server, "GET", "/show/999", cookie=cookie)
    assert resp.status == 404

    resp, body = request(server, "GET", "/shows", cookie=cookie)
    assert resp.status == 200 and "href='/show/1'>Show A</a>" in body

    resp, body = request(server, "GET", "/search?q=somerton", cookie=cookie)
    assert resp.status == 200 and "Somerton Man" in body

    resp, body = request(server, "GET", "/topics?genre=mystery", cookie=cookie)
    assert resp.status == 200 and "Somerton Man" in body


def test_index_status_shows_pending_episodes(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Show A', 'http://x')")
    conn.executemany(
        "INSERT INTO episodes (show_id, guid, title, extracted_at) VALUES (1, ?, ?, ?)",
        [("g1", "Done", "2026-01-01T00:00:00Z"), ("g2", "Pending", None)],
    )
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/", cookie=cookie)
        assert "1 episode not yet indexed" in body
        assert "Indexing in progress" not in body  # last activity is months old, not active
    finally:
        srv.shutdown()


def test_index_status_shows_active_when_recently_processed(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Show A', 'http://x')")
    conn.executemany(
        "INSERT INTO episodes (show_id, guid, title, extracted_at) VALUES (1, ?, ?, ?)",
        [("g1", "Done", web.iso(web.utcnow())), ("g2", "Pending", None)],
    )
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/", cookie=cookie)
        assert "Indexing in progress" in body
        assert 'class="status active"' in body
    finally:
        srv.shutdown()


def test_pipeline_status_shows_ad_stripping_and_comparison_backlog(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Show A', 'http://x')")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('r', 'Show B', 'http://y')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, audio_url) VALUES (1, 'g1', 'Untranscribed', 'http://a/1.mp3')"
    )
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1, 'g2', 'Undetected', '/t/2.json')"
    )
    # Two shows' episodes on the same topic, both transcribed -> pending comparison.
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1, 'g3', 'Cmp A', '/t/3.json')"
    )
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (2, 'g4', 'Cmp B', '/t/4.json')"
    )
    conn.execute("INSERT INTO topics (label) VALUES ('Shared Case')")
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, 1, 't')", [(3,), (4,)]
    )
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/", cookie=cookie)
        assert resp.status == 200
        assert "1 episode awaiting transcription" in body
        assert "3 episodes awaiting ad-span detection" in body  # Undetected, Cmp A, Cmp B
        assert "1 topic ready for cross-show claims comparison" in body
    finally:
        srv.shutdown()


def test_pipeline_status_absent_when_nothing_pending(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Show A', 'http://x')")
    # No audio_url (nothing to transcribe), no transcript (nothing to
    # detect), no ad_segments (nothing to cut), only 1 show (never eligible
    # for comparison) — the pipeline has genuinely nothing to report.
    conn.execute("INSERT INTO episodes (show_id, guid, title) VALUES (1, 'g1', 'Untouched')")
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/", cookie=cookie)
        assert "awaiting transcription" not in body
        assert "awaiting ad-span detection" not in body
        assert "ready for cross-show claims comparison" not in body
    finally:
        srv.shutdown()


def test_show_page_shows_ad_stripping_progress(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Show A', 'http://x')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path, llm_detected_at, cut_path)"
        " VALUES (1, 'g1', 'Done', '/t/1.json', '2026-01-01T00:00:00Z', '/c/1.mp3')"
    )
    conn.execute("INSERT INTO episodes (show_id, guid, title) VALUES (1, 'g2', 'Untouched')")
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/show/1", cookie=cookie)
        assert resp.status == 200
        assert "1/2 transcribed, 1/2 ad-scanned, 1/2 cut." in body
    finally:
        srv.shutdown()


def test_topic_page_shows_comparison_pending_note(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Show A', 'http://x')")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('r', 'Show B', 'http://y')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1, 'g1', 'Ep A', '/t/1.json')"
    )
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (2, 'g2', 'Ep B', '/t/2.json')"
    )
    conn.execute("INSERT INTO topics (label) VALUES ('Shared Case')")
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, 1, 't')", [(1,), (2,)]
    )
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/topic/1", cookie=cookie)
        assert resp.status == 200
        assert "not compared yet" in body
    finally:
        srv.shutdown()


def test_topic_page_shows_comparison_available_note(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Show A', 'http://x')")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('r', 'Show B', 'http://y')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1, 'g1', 'Ep A', '/t/1.json')"
    )
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (2, 'g2', 'Ep B', '/t/2.json')"
    )
    conn.execute("INSERT INTO topics (label) VALUES ('Shared Case')")
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, 1, 't')", [(1,), (2,)]
    )
    conn.commit()
    conn.close()
    claims.load_comparisons(
        db.connect(tmp_path / "hark.db"),
        [{"topic_id": 1, "shared": ["fact"], "unique_by_show": {}}],
    )
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/topic/1", cookie=cookie)
        assert resp.status == 200
        assert "Cross-show claims comparison available" in body
        assert "not compared yet" not in body
    finally:
        srv.shutdown()


def test_contested_topics_ranks_by_unique_claim_count(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO topics (id, label) VALUES (1, 'Low Divergence')")
    conn.execute("INSERT INTO topics (id, label) VALUES (2, 'High Divergence')")
    conn.execute("INSERT INTO topic_genres (topic_id, genre) VALUES (2, 'mystery')")
    conn.commit()
    claims.load_comparisons(conn, [
        {"topic_id": 1, "shared": ["a", "b", "c"], "unique_by_show": {"Show A": ["x"]}},
        {"topic_id": 2, "shared": ["a"], "unique_by_show": {"Show A": ["x", "y"], "Show B": ["z"]}},
    ])
    result = web.contested_topics(conn, limit=10)
    assert [r["topic_id"] for r in result] == [2, 1]
    assert result[0]["unique_count"] == 3
    assert result[0]["shared_count"] == 1
    assert result[0]["genres"] == ["mystery"]
    conn.close()


def test_rare_genre_episodes_picks_two_least_common_genres(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title) VALUES ('q', 'Show A')")
    conn.executemany(
        "INSERT INTO topics (id, label) VALUES (?, ?)",
        [(1, "Common"), (2, "Common2"), (3, "Common3"), (4, "Rare"), (5, "Rarest")],
    )
    # 'history' covers 3 topics, 'cult' covers 1, 'espionage' covers 1 -> the
    # two least common should be cult and espionage (order between equally
    # rare genres isn't asserted).
    conn.executemany(
        "INSERT INTO topic_genres (topic_id, genre) VALUES (?, ?)",
        [(1, "history"), (2, "history"), (3, "history"), (4, "cult"), (5, "espionage")],
    )
    conn.executemany(
        "INSERT INTO episodes (show_id, guid, title) VALUES (1, ?, ?)",
        [("g4", "Rare Ep"), ("g5", "Rarest Ep")],
    )
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, ?, 't')",
        [(1, 4), (2, 5)],
    )
    conn.commit()
    genres, rows = web.rare_genre_episodes(conn, limit=10)
    assert set(genres) == {"cult", "espionage"}
    assert {r["title"] for r in rows} == {"Rare Ep", "Rarest Ep"}
    conn.close()


def test_notable_page_shows_both_sections(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title) VALUES ('q', 'Show A')")
    conn.execute("INSERT INTO topics (id, label) VALUES (1, 'Contested Case')")
    conn.execute("INSERT INTO topics (id, label) VALUES (2, 'Rare Case')")
    conn.execute("INSERT INTO topic_genres (topic_id, genre) VALUES (2, 'cult')")
    conn.execute("INSERT INTO episodes (show_id, guid, title) VALUES (1, 'g1', 'Rare Episode')")
    conn.execute("INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (1, 2, 't')")
    conn.commit()
    claims.load_comparisons(conn, [
        {"topic_id": 1, "shared": ["a"], "unique_by_show": {"Show A": ["x"]}},
    ])
    conn.close()

    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/notable", cookie=cookie)
        assert resp.status == 200
        assert 'href=\'/topic/1\'>Contested Case</a>' in body
        assert 'href=\'/episode/1\'>Rare Episode</a>' in body
    finally:
        srv.shutdown()


def test_notable_page_handles_empty_state(server):
    cookie = login(server)
    resp, body = request(server, "GET", "/notable", cookie=cookie)
    assert resp.status == 200
    assert "No claims comparisons loaded yet" in body


def test_show_page_distinguishes_unindexed_from_topicless_episodes(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Show A', 'http://x')")
    conn.executemany(
        "INSERT INTO episodes (show_id, guid, title, extracted_at) VALUES (1, ?, ?, ?)",
        [
            ("g1", "Has topic", "2026-01-01T00:00:00Z"),
            ("g2", "Trailer, no subject", "2026-01-01T00:00:00Z"),
            ("g3", "Not indexed yet", None),
        ],
    )
    conn.execute("INSERT INTO topics (label) VALUES ('Something')")
    conn.execute("INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (1, 1, 't')")
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/show/1", cookie=cookie)
        assert resp.status == 200
        # episode with a topic: pill, no "not yet indexed" note
        has_topic_row = body.split("Has topic")[1].split("</tr>")[0]
        assert 'href="/topic/1">Something</a>' in has_topic_row
        assert "not yet indexed" not in has_topic_row
        # extracted but genuinely topic-less: neither a pill nor the note
        topicless_row = body.split("Trailer, no subject")[1].split("</tr>")[0]
        assert "pill" not in topicless_row and "not yet indexed" not in topicless_row
        # never extracted: the note, no pill
        pending_row = body.split("Not indexed yet")[1].split("</tr>")[0]
        assert "not yet indexed" in pending_row and "pill" not in pending_row
    finally:
        srv.shutdown()


def test_related_topics_ranks_by_co_occurring_episode_count(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('a', 'Show A', 'http://a')")
    conn.executemany(
        "INSERT INTO topics (label) VALUES (?)", [("Fred West",), ("Rosemary West",), ("Nilsen",)]
    )
    # Two episodes mention both Fred West and Rosemary West
    conn.execute("INSERT INTO episodes (show_id, guid, title) VALUES (1, 'g1', 'Ep 1')")
    conn.execute("INSERT INTO episodes (show_id, guid, title) VALUES (1, 'g2', 'Ep 2')")
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, ?, 't')",
        [(1, 1), (1, 2), (2, 1), (2, 2)],
    )
    # One episode mentions Fred West and Nilsen
    conn.execute("INSERT INTO episodes (show_id, guid, title) VALUES (1, 'g3', 'Ep 3')")
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, ?, 't')",
        [(3, 1), (3, 3)],
    )
    conn.commit()

    related = web.related_topics(conn, topic_id=1)
    assert [(r["label"], r["episodes"]) for r in related] == [("Rosemary West", 2), ("Nilsen", 1)]
    conn.close()


def test_topic_page_shows_related_topics_section(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('a', 'Show A', 'http://a')")
    conn.execute("INSERT INTO topics (label) VALUES ('Fred West')")
    conn.execute("INSERT INTO topics (label) VALUES ('Rosemary West')")
    conn.execute("INSERT INTO episodes (show_id, guid, title) VALUES (1, 'g1', 'Ep 1')")
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (1, ?, 't')",
        [(1,), (2,)],
    )
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/topic/1", cookie=cookie)
        assert resp.status == 200
        assert "Related topics" in body
        assert 'href="/topic/2">Rosemary West (1 episode)</a>' in body
    finally:
        srv.shutdown()


def test_topic_with_no_co_occurrence_omits_related_topics_section(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('a', 'Show A', 'http://a')")
    conn.execute("INSERT INTO topics (label) VALUES ('Solo Topic')")
    conn.execute("INSERT INTO episodes (show_id, guid, title) VALUES (1, 'g1', 'Ep 1')")
    conn.execute("INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (1, 1, 't')")
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/topic/1", cookie=cookie)
        assert resp.status == 200
        assert "Related topics" not in body
    finally:
        srv.shutdown()


def test_related_shows_ranks_by_shared_topic_count(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('a', 'Show A', 'http://a')")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('b', 'Show B', 'http://b')")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('c', 'Show C', 'http://c')")
    conn.executemany(
        "INSERT INTO topics (label) VALUES (?)", [("T1",), ("T2",), ("T3",)]
    )
    # Show A episode covering T1, T2, T3
    conn.execute("INSERT INTO episodes (show_id, guid, title) VALUES (1, 'ga', 'Ep A')")
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (1, ?, 't')",
        [(1,), (2,), (3,)],
    )
    # Show B covers T1, T2 (2 shared with A) -> should rank above Show C
    conn.execute("INSERT INTO episodes (show_id, guid, title) VALUES (2, 'gb', 'Ep B')")
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (2, ?, 't')",
        [(1,), (2,)],
    )
    # Show C covers only T1 (1 shared with A)
    conn.execute("INSERT INTO episodes (show_id, guid, title) VALUES (3, 'gc', 'Ep C')")
    conn.execute("INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (3, 1, 't')")
    conn.commit()

    related = web.related_shows(conn, show_id=1)
    assert [(r["name"], r["shared"]) for r in related] == [("Show B", 2), ("Show C", 1)]
    conn.close()


def test_show_page_shows_related_shows_section(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('a', 'Show A', 'http://a')")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('b', 'Show B', 'http://b')")
    conn.execute("INSERT INTO topics (label) VALUES ('Shared Topic')")
    conn.execute("INSERT INTO episodes (show_id, guid, title) VALUES (1, 'ga', 'Ep A')")
    conn.execute("INSERT INTO episodes (show_id, guid, title) VALUES (2, 'gb', 'Ep B')")
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, 1, 't')",
        [(1,), (2,)],
    )
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/show/1", cookie=cookie)
        assert resp.status == 200
        assert "Related shows" in body
        assert 'href="/show/2">Show B (1 shared topic)</a>' in body
    finally:
        srv.shutdown()


def test_show_with_no_overlap_omits_related_shows_section(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('a', 'Show A', 'http://a')")
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/show/1", cookie=cookie)
        assert resp.status == 200
        assert "Related shows" not in body
    finally:
        srv.shutdown()


def test_show_page_paginates_episodes(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Big Show', 'http://x')")
    conn.executemany(
        "INSERT INTO episodes (show_id, guid, title, pubdate, extracted_at) VALUES (1, ?, ?, ?, ?)",
        [(f"g{i}", f"Episode {i}", f"2020-01-{i:02d}T00:00:00Z", "2026-01-01T00:00:00Z")
         for i in range(1, 61)],  # 60 episodes, one more page than PAGE_SIZE (50)
    )
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]

        resp, body = request(srv, "GET", "/show/1", cookie=cookie)
        assert resp.status == 200
        assert "60 episodes" in body  # total is the real count, not the page size
        assert body.count("<tr><td>") == web.PAGE_SIZE  # only one page's worth rendered
        assert "page 1 of 2" in body

        resp, body = request(srv, "GET", "/show/1?page=2", cookie=cookie)
        assert resp.status == 200
        assert body.count("<tr><td>") == 10  # remainder on the second page
        assert "page 2 of 2" in body
        assert "&laquo; prev" in body and 'href="/show/1?page=1"' in body

        # out-of-range page clamps to the last page instead of rendering empty
        resp, body = request(srv, "GET", "/show/1?page=999", cookie=cookie)
        assert resp.status == 200 and "page 2 of 2" in body
    finally:
        srv.shutdown()


def test_topics_page_paginates(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Show', 'http://x')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, extracted_at) VALUES (1, 'g', 'ep', '2026-01-01T00:00:00Z')"
    )
    for i in range(1, 55):  # 54 topics, more than one page
        conn.execute("INSERT INTO topics (label) VALUES (?)", (f"Topic {i:02d}",))
        conn.execute(
            "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (1, ?, 't')", (i,)
        )
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/topics", cookie=cookie)
        assert resp.status == 200 and "page 1 of 2" in body
        resp, body = request(srv, "GET", "/topics?page=2", cookie=cookie)
        assert resp.status == 200 and "page 2 of 2" in body
    finally:
        srv.shutdown()


def test_search_no_matches_shows_specific_message(server):
    cookie = login(server)
    resp, body = request(server, "GET", "/search?q=zzznonexistentzzz", cookie=cookie)
    assert resp.status == 200
    assert "No topics match" in body and "zzznonexistentzzz" in body
    assert "No episode titles match" in body
    assert "Nothing here yet." not in body  # generic empty-db message must not leak in


def test_home_page_view_all_topics_link(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Show', 'http://x')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, extracted_at) VALUES (1, 'g', 'ep', '2026-01-01T00:00:00Z')"
    )
    for i in range(1, 17):  # 16 topics: one more than the home page's top-15 widget
        conn.execute("INSERT INTO topics (label) VALUES (?)", (f"Topic {i:02d}",))
        conn.execute(
            "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (1, ?, 't')", (i,)
        )
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/", cookie=cookie)
        assert resp.status == 200
        assert 'href="/topics">view all 16 topics' in body
    finally:
        srv.shutdown()


def test_show_page_shows_feed_url_and_adblock_toggle(server, tmp_path):
    cookie = login(server)
    conn = db.connect(tmp_path / "hark.db")
    token = conn.execute("SELECT feed_token FROM shows WHERE id = 1").fetchone()["feed_token"]
    conn.close()

    resp, body = request(server, "GET", "/show/1", cookie=cookie)
    assert resp.status == 200
    assert f"http://localhost:8710/feed/1/{token}" in body
    assert "<strong>enabled</strong>" in body  # default
    assert "Disable ad-stripping" in body


def test_adblock_toggle_flips_state_and_redirects(server):
    cookie = login(server)
    resp, _ = request(server, "POST", "/show/1/adblock", body={}, cookie=cookie)
    assert resp.status == 303
    assert resp.getheader("Location") == "/show/1"

    resp, body = request(server, "GET", "/show/1", cookie=cookie)
    assert "<strong>disabled</strong>" in body
    assert "Enable ad-stripping" in body

    # toggling again flips it back
    request(server, "POST", "/show/1/adblock", body={}, cookie=cookie)
    resp, body = request(server, "GET", "/show/1", cookie=cookie)
    assert "<strong>enabled</strong>" in body


def test_adblock_toggle_is_atomic_under_concurrent_requests(server):
    # Regression test for a read-modify-write race: the old code read the
    # current state in Python, flipped it, then wrote it back — two
    # concurrent toggles could both read the same starting value and
    # collapse into one net change instead of canceling out. The fix
    # computes the flip in a single SQL UPDATE. 20 concurrent toggles from
    # an even starting state (enabled) must land back on enabled — any lost
    # update would make this flaky/wrong.
    import threading

    cookie = login(server)
    errors = []

    def toggle():
        try:
            resp, _ = request(server, "POST", "/show/1/adblock", body={}, cookie=cookie)
            if resp.status != 303:
                errors.append(resp.status)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=toggle) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    resp, body = request(server, "GET", "/show/1", cookie=cookie)
    assert "<strong>enabled</strong>" in body  # 20 (even) toggles from enabled -> enabled


def test_adblock_toggle_404_for_missing_show(server):
    cookie = login(server)
    resp, _ = request(server, "POST", "/show/999/adblock", body={}, cookie=cookie)
    assert resp.status == 404


def test_adblock_toggle_requires_login(server):
    resp, _ = request(server, "POST", "/show/1/adblock", body={})
    assert resp.status == 303
    assert resp.getheader("Location") == "/login"


def test_show_page_shows_topic_index_toggle(server):
    cookie = login(server)
    resp, body = request(server, "GET", "/show/1", cookie=cookie)
    assert resp.status == 200
    assert "Topic index for this show: <strong>enabled</strong>" in body  # server fixture default
    assert "Remove from topic index" in body


def test_topic_index_toggle_flips_state_and_redirects(server):
    cookie = login(server)
    resp, _ = request(server, "POST", "/show/1/topic-index", body={}, cookie=cookie)
    assert resp.status == 303
    assert resp.getheader("Location") == "/show/1"

    resp, body = request(server, "GET", "/show/1", cookie=cookie)
    assert "Topic index for this show: <strong>disabled</strong>" in body
    assert "Add to topic index" in body
    assert "won't be checked for a real-world subject" in body

    request(server, "POST", "/show/1/topic-index", body={}, cookie=cookie)
    resp, body = request(server, "GET", "/show/1", cookie=cookie)
    assert "Topic index for this show: <strong>enabled</strong>" in body


def test_topic_index_toggle_404_for_missing_show(server):
    cookie = login(server)
    resp, _ = request(server, "POST", "/show/999/topic-index", body={}, cookie=cookie)
    assert resp.status == 404


def test_topic_index_toggle_requires_login(server):
    resp, _ = request(server, "POST", "/show/1/topic-index", body={})
    assert resp.status == 303


def test_shows_page_flags_unreviewed_shows(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Curated', 'http://x')")
    conn.execute(
        "INSERT INTO shows (query, title, feed_url, topic_index_enabled)"
        " VALUES ('http://y', 'Unreviewed', 'http://y', 0)"
    )
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/shows?all=1", cookie=cookie)
        assert resp.status == 200
        assert "1 show not yet reviewed for the topic index" in body
        unreviewed_row = body.split("Unreviewed")[1].split("</tr>")[0]
        assert "unreviewed" in unreviewed_row
        curated_row = body.split("Curated")[1].split("</tr>")[0]
        assert "unreviewed" not in curated_row
    finally:
        srv.shutdown()


def test_plural():
    assert web.plural(0, "episode") == "0 episodes"
    assert web.plural(1, "episode") == "1 episode"
    assert web.plural(2, "episode") == "2 episodes"


def test_episode_page_404_for_missing_episode(server):
    cookie = login(server)
    resp, _ = request(server, "GET", "/episode/999", cookie=cookie)
    assert resp.status == 404


def test_episode_page_notes_by_comparison_state(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('a', 'Show A', 'http://a')")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('b', 'Show B', 'http://b')")
    conn.execute("INSERT INTO topics (label) VALUES ('No Transcript Yet')")
    conn.execute("INSERT INTO topics (label) VALUES ('One Show Only')")
    conn.execute("INSERT INTO topics (label) VALUES ('Two Shows, Not Compared')")
    conn.executemany(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (?, ?, ?, ?)",
        [
            (1, "g1", "Ep No Transcript", None),
            (1, "g2", "Ep One Show", "/tmp/t2.json"),
            (1, "g3", "Ep Two Shows A", "/tmp/t3a.json"),
            (2, "g4", "Ep Two Shows B", "/tmp/t3b.json"),
        ],
    )
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, ?, 't')",
        [(1, 1), (2, 2), (3, 3), (4, 3)],
    )
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]

        resp, body = request(srv, "GET", "/episode/1", cookie=cookie)
        assert resp.status == 200
        assert "hasn’t been transcribed yet" in body

        resp, body = request(srv, "GET", "/episode/2", cookie=cookie)
        assert resp.status == 200
        assert "Only this show has covered this topic so far" in body

        resp, body = request(srv, "GET", "/episode/3", cookie=cookie)
        assert resp.status == 200
        assert "not compared yet" in body
    finally:
        srv.shutdown()


def test_episode_page_renders_stored_comparison(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('a', 'Show A', 'http://a')")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('b', 'Show B', 'http://b')")
    conn.execute("INSERT INTO topics (label) VALUES ('Somerton Man')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (1, 'g1', 'A tells it', '/tmp/a.json')"
    )
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, transcript_path) VALUES (2, 'g2', 'B tells it', '/tmp/b.json')"
    )
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, 1, 't')", [(1,), (2,)]
    )
    conn.commit()
    claims.load_comparisons(conn, [{
        "topic_id": 1,
        "shared": ["the body was never identified"],
        "unique_by_show": {"Show A": ["mentions the Rubaiyat code"], "Show B": []},
    }])
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]

        resp, body = request(srv, "GET", "/episode/1", cookie=cookie)
        assert resp.status == 200
        assert "the body was never identified" in body
        assert "mentions the Rubaiyat code" in body
        assert "Show A (this episode)" in body
        # Show B had no unique claims (empty list) so it shouldn't render a heading
        assert "Unique to Show B" not in body

        # from the other show's episode, the "(this episode)" tag follows *that* episode
        resp, body = request(srv, "GET", "/episode/2", cookie=cookie)
        assert "Show A (this episode)" not in body
        assert "Unique to Show A:" in body
    finally:
        srv.shutdown()


def test_episode_links_reachable_from_topic_and_show_pages(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'Show A', 'http://x')")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, extracted_at) VALUES"
        " (1, 'g1', 'Case 1', '2026-01-01T00:00:00Z')"
    )
    conn.execute("INSERT INTO topics (label) VALUES ('Some Topic')")
    conn.execute("INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (1, 1, 't')")
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        for path in ("/show/1", "/topic/1", "/"):
            resp, body = request(srv, "GET", path, cookie=cookie)
            assert "/episode/1" in body, path
    finally:
        srv.shutdown()


def test_bad_login_rejected(server):
    resp, _ = request(server, "POST", "/login", body={"username": "admin", "password": "wrong"})
    assert resp.status == 401
    resp, _ = request(server, "POST", "/login", body={"username": "ghost", "password": "letmein"})
    assert resp.status == 401


def test_fail_closed_without_token(tmp_path):
    db.connect(tmp_path / "hark.db").close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": ""})
        assert resp.status == 401
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "anything"})
        assert resp.status == 401
    finally:
        srv.shutdown()


def test_missing_hark_db_returns_503_not_crash(tmp_path):
    # hark.db is never created — simulates a fresh volume before any ingest.
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        cookie = login(srv, password="t")
        resp, body = request(srv, "GET", "/", cookie=cookie)
        assert resp.status == 503
        assert "Not ready" in body
        # /healthz never touches hark.db, so it stays healthy regardless
        resp, _ = request(srv, "GET", "/healthz")
        assert resp.status == 200
    finally:
        srv.shutdown()


def test_post_without_session_drains_body_no_keepalive_desync(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    body = urllib.parse.urlencode({"password": "x", "password2": "x"})
    conn.request("POST", "/account/password", body=body,
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp1 = conn.getresponse()
    assert resp1.status == 303  # no session -> redirected to /login
    resp1.read()

    # If the body above wasn't drained, this next request on the same
    # connection would be parsed starting mid-body and come back mangled.
    conn.request("GET", "/healthz")
    resp2 = conn.getresponse()
    assert resp2.status == 200
    assert resp2.read() == b"ok"
    conn.close()


def test_password_change_revokes_sessions_and_disables_token(server):
    cookie = login(server)
    resp, _ = request(server, "POST", "/account/password",
                      body={"password": "hunter2hunter2", "password2": "hunter2hunter2"},
                      cookie=cookie)
    assert resp.status == 303

    # old session is dead
    resp, _ = request(server, "GET", "/", cookie=cookie)
    assert resp.status == 303 and resp.getheader("Location") == "/login"

    # admin token no longer works once a password is set
    resp, _ = request(server, "POST", "/login", body={"username": "admin", "password": "letmein"})
    assert resp.status == 401

    cookie = login(server, password="hunter2hunter2")
    resp, _ = request(server, "GET", "/", cookie=cookie)
    assert resp.status == 200


def test_logout_kills_session(server):
    cookie = login(server)
    resp, _ = request(server, "POST", "/logout", cookie=cookie)
    assert resp.status == 303
    resp, _ = request(server, "GET", "/", cookie=cookie)
    assert resp.status == 303


def test_session_cookie_flags(server):
    resp, _ = request(server, "POST", "/login", body={"username": "admin", "password": "letmein"})
    flags = resp.getheader("Set-Cookie")
    assert "HttpOnly" in flags and "SameSite=Lax" in flags
    assert "Secure" not in flags  # LAN default; HARK_COOKIE_SECURE=1 enables it


def test_cookie_secure_flag(tmp_path):
    db.connect(tmp_path / "hark.db").close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t", cookie_secure=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        assert "Secure" in resp.getheader("Set-Cookie")
    finally:
        srv.shutdown()


def test_audio_link_scheme_allowlist(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', 'S', 'http://x')")
    conn.executemany(
        "INSERT INTO episodes (show_id, guid, title, audio_url, extracted_at)"
        " VALUES (1, ?, ?, ?, '2026-01-01T00:00:00Z')",
        [
            ("g1", "ok", "https://cdn/a.mp3"),
            ("g2", "evil", "javascript:alert(1)"),
        ],
    )
    conn.execute("INSERT INTO topics (label) VALUES ('T')")
    conn.executemany(
        "INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (?, 1, 't')",
        [(1,), (2,)],
    )
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/topic/1", cookie=cookie)
        assert 'href="https://cdn/a.mp3"' in body
        assert "javascript:" not in body
    finally:
        srv.shutdown()


def test_html_escapes_labels(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('q', '<b>S</b>', 'http://x')")
    conn.execute("INSERT INTO episodes (show_id, guid, title, extracted_at)"
                 " VALUES (1, 'g', '<script>alert(1)</script>', '2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO topics (label) VALUES ('<img src=x onerror=alert(1)>')")
    conn.execute("INSERT INTO episode_topics (episode_id, topic_id, source) VALUES (1, 1, 't')")
    conn.commit()
    conn.close()
    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="t")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp, _ = request(srv, "POST", "/login", body={"username": "admin", "password": "t"})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        resp, body = request(srv, "GET", "/topic/1", cookie=cookie)
        assert resp.status == 200
        assert "<script>" not in body and "<img src=x" not in body
        assert "&lt;script&gt;" in body
    finally:
        srv.shutdown()


# --- gpodder-sync server (AntennaPod-compatible endpoints) ---

GPODDER_PATHS = [
    ("GET", "/index.php/apps/gpoddersync/subscriptions"),
    ("POST", "/index.php/apps/gpoddersync/subscription_change/create"),
    ("GET", "/index.php/apps/gpoddersync/episode_action"),
    ("POST", "/index.php/apps/gpoddersync/episode_action/create"),
]


def test_gpodder_endpoints_require_basic_auth(server):
    for method, path in GPODDER_PATHS:
        resp, _ = request(server, method, path, json_body={} if method == "POST" else None)
        assert resp.status == 401, path
        assert resp.getheader("WWW-Authenticate", "").startswith("Basic")


def test_gpodder_endpoints_reject_bad_credentials(server):
    resp, _ = request(server, "GET", "/index.php/apps/gpoddersync/subscriptions",
                      auth=("admin", "wrong-password"))
    assert resp.status == 401


def test_gpodder_endpoints_accept_cookie_login_credentials(server):
    # Same account as the web UI (Auth.verify) — no separate credential to manage.
    resp, body = request(server, "GET", "/index.php/apps/gpoddersync/subscriptions",
                         auth=("admin", "letmein"))
    assert resp.status == 200
    data = json.loads(body)
    assert data["add"] == [] and data["remove"] == []
    assert isinstance(data["timestamp"], int) and data["timestamp"] > 0


def test_gpodder_subscription_round_trip(server):
    auth = ("admin", "letmein")
    resp, body = request(server, "POST", "/index.php/apps/gpoddersync/subscription_change/create",
                         auth=auth, json_body={"add": ["https://new.example/feed"], "remove": []})
    assert resp.status == 200
    assert "timestamp" in json.loads(body)

    resp, body = request(server, "GET", "/index.php/apps/gpoddersync/subscriptions",
                         auth=auth, json_body=None)
    data = json.loads(body)
    assert data["add"] == ["https://new.example/feed"]


def test_gpodder_subscription_change_registers_show_unreviewed(server, tmp_path):
    auth = ("admin", "letmein")
    request(server, "POST", "/index.php/apps/gpoddersync/subscription_change/create",
            auth=auth, json_body={"add": ["https://new.example/feed"], "remove": []})
    conn = db.connect(tmp_path / "hark.db")
    row = conn.execute(
        "SELECT topic_index_enabled FROM shows WHERE feed_url = ?", ("https://new.example/feed",)
    ).fetchone()
    assert row["topic_index_enabled"] == 0


def test_gpodder_subscription_change_rejects_non_object_body(server):
    resp, _ = request(server, "POST", "/index.php/apps/gpoddersync/subscription_change/create",
                      auth=("admin", "letmein"), json_body=["not", "an", "object"])
    assert resp.status == 400


def test_gpodder_episode_action_round_trip(server):
    auth = ("admin", "letmein")
    action = {"podcast": "https://a.example/feed", "episode": "https://a.example/ep1.mp3",
              "action": "play", "guid": "g1", "timestamp": "2026-01-01T00:00:00",
              "started": 0, "position": 42, "total": 1000}
    resp, _ = request(server, "POST", "/index.php/apps/gpoddersync/episode_action/create",
                      auth=auth, json_body=[action])
    assert resp.status == 200

    resp, body = request(server, "GET", "/index.php/apps/gpoddersync/episode_action?since=0",
                         auth=auth, json_body=None)
    assert resp.status == 200
    data = json.loads(body)
    assert data["actions"] == [action]


def test_gpodder_episode_action_rejects_non_array_body(server):
    resp, _ = request(server, "POST", "/index.php/apps/gpoddersync/episode_action/create",
                      auth=("admin", "letmein"), json_body={"not": "an array"})
    assert resp.status == 400


# --- Auth: user management (0.14.0) ---

def test_bootstrap_admin_is_admin(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    row = auth.list_users()[0]
    assert row["username"] == "admin" and row["is_admin"] == 1


def test_create_user_defaults_to_non_admin(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    auth.create_user("alice")
    row = [u for u in auth.list_users() if u["username"] == "alice"][0]
    assert row["is_admin"] == 0
    assert row["has_password"] == 0


def test_create_user_can_grant_admin(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    auth.create_user("bob", is_admin=True)
    row = [u for u in auth.list_users() if u["username"] == "bob"][0]
    assert row["is_admin"] == 1


def test_create_user_rejects_duplicate_username(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    auth.create_user("alice")
    with pytest.raises(Exception):
        auth.create_user("alice")


def test_new_user_can_log_in_with_bootstrap_token(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    user_id = auth.create_user("alice")
    assert auth.verify("alice", "t") == user_id


def test_delete_user_removes_account(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    auth.create_user("alice")
    assert auth.delete_user("alice") is True
    assert "alice" not in [u["username"] for u in auth.list_users()]


def test_delete_user_returns_false_for_unknown_user(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    assert auth.delete_user("nope") is False


def test_delete_user_revokes_sessions(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    user_id = auth.create_user("alice")
    token = auth.create_session(user_id)
    auth.delete_user("alice")
    assert auth.session_user(token) is None


def test_is_admin_true_for_admin_account(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    admin_id = auth.list_users()[0]["id"]
    assert auth.is_admin(admin_id) is True


def test_is_admin_false_for_regular_account(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    user_id = auth.create_user("alice")
    assert auth.is_admin(user_id) is False


def test_set_password_only_revokes_that_accounts_sessions(tmp_path):
    # Regression: set_password used to run a bare `DELETE FROM sessions`
    # with no WHERE clause — harmless pre-multi-user (only one account's
    # sessions ever existed), but would silently log out every other
    # account the moment any one of them changed a password.
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    admin_id = auth.list_users()[0]["id"]
    alice_id = auth.create_user("alice")
    admin_session = auth.create_session(admin_id)
    auth.set_password(alice_id, "alice-password")
    assert auth.session_user(admin_session) is not None


# --- Invite links (0.14.0) ---

def test_create_invite_returns_working_token(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    user_id, token = auth.create_invite("alice")
    invite = auth.find_by_invite_token(token)
    assert invite is not None
    assert invite["id"] == user_id and invite["username"] == "alice"


def test_create_invite_defaults_to_non_admin(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    auth.create_invite("alice")
    row = [u for u in auth.list_users() if u["username"] == "alice"][0]
    assert row["is_admin"] == 0
    assert row["invite_pending"] == 1


def test_create_invite_can_grant_admin(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    auth.create_invite("alice", is_admin=True)
    row = [u for u in auth.list_users() if u["username"] == "alice"][0]
    assert row["is_admin"] == 1


def test_find_by_invite_token_unknown_returns_none(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    assert auth.find_by_invite_token("not-a-real-token") is None


def test_find_by_invite_token_expired_returns_none(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    _, token = auth.create_invite("alice")
    # Simulate time passing past the expiry window rather than monkeypatching
    # utcnow() globally (find_by_invite_token compares against the stored
    # invite_expires_at, so backdating that column has the same effect).
    with contextlib.closing(auth._connect()) as conn:
        conn.execute(
            "UPDATE users SET invite_expires_at = '2020-01-01T00:00:00Z' WHERE username = 'alice'"
        )
        conn.commit()
    assert auth.find_by_invite_token(token) is None


def test_accept_invite_sets_password_and_clears_token(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    user_id, token = auth.create_invite("alice")
    accepted_id = auth.accept_invite(token, "a-real-password")
    assert accepted_id == user_id
    assert auth.verify("alice", "a-real-password") == user_id
    assert auth.find_by_invite_token(token) is None  # single-use


def test_accept_invite_unknown_token_returns_none(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    assert auth.accept_invite("bogus", "a-real-password") is None


def test_accept_invite_cannot_be_reused(tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="t")
    _, token = auth.create_invite("alice")
    auth.accept_invite(token, "first-password")
    assert auth.accept_invite(token, "second-password") is None


# --- Multi-user: web UI + gpodder-sync isolation (0.14.0) ---

def test_shows_page_defaults_to_my_list_only(server, tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('r', 'Not Mine', 'http://z')")
    conn.commit()
    conn.close()
    cookie = login(server)
    resp, body = request(server, "GET", "/shows", cookie=cookie)
    assert resp.status == 200
    assert "Show A" in body  # subscribed via the fixture's user_shows row
    assert "Not Mine" not in body


def test_shows_page_all_param_shows_everything(server, tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('r', 'Not Mine', 'http://z')")
    conn.commit()
    conn.close()
    cookie = login(server)
    resp, body = request(server, "GET", "/shows?all=1", cookie=cookie)
    assert resp.status == 200
    assert "Show A" in body and "Not Mine" in body
    assert "not in my list" in body  # pill on the unsubscribed one


def test_subscribe_adds_show_to_my_list(server, tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_url) VALUES ('r', 'Other Show', 'http://z')")
    conn.commit()
    conn.close()
    cookie = login(server)
    resp, _ = request(server, "POST", "/show/2/subscribe", body={}, cookie=cookie)
    assert resp.status == 303
    resp, body = request(server, "GET", "/shows", cookie=cookie)
    assert "Other Show" in body


def test_unsubscribe_removes_show_from_my_list(server):
    cookie = login(server)
    resp, _ = request(server, "POST", "/show/1/unsubscribe", body={}, cookie=cookie)
    assert resp.status == 303
    resp, body = request(server, "GET", "/shows", cookie=cookie)
    assert "Show A" not in body


def test_unsubscribe_never_deletes_the_global_show(server, tmp_path):
    cookie = login(server)
    request(server, "POST", "/show/1/unsubscribe", body={}, cookie=cookie)
    conn = db.connect(tmp_path / "hark.db")
    assert conn.execute("SELECT COUNT(*) FROM shows WHERE id = 1").fetchone()[0] == 1


def test_subscribe_unknown_show_404s(server):
    cookie = login(server)
    resp, _ = request(server, "POST", "/show/999/subscribe", body={}, cookie=cookie)
    assert resp.status == 404


def test_show_page_offers_subscribe_toggle(server):
    cookie = login(server)
    resp, body = request(server, "GET", "/show/1", cookie=cookie)
    assert "In your list." in body
    assert 'action="/show/1/unsubscribe"' in body


def test_non_admin_cannot_toggle_ad_stripping(server, tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="letmein")
    auth.create_user("viewer")
    resp, _ = request(server, "POST", "/login", body={"username": "viewer", "password": "letmein"})
    cookie = resp.getheader("Set-Cookie").split(";")[0]
    resp, _ = request(server, "POST", "/show/1/adblock", body={}, cookie=cookie)
    assert resp.status == 403


def test_non_admin_cannot_toggle_topic_index(server, tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="letmein")
    auth.create_user("viewer")
    resp, _ = request(server, "POST", "/login", body={"username": "viewer", "password": "letmein"})
    cookie = resp.getheader("Set-Cookie").split(";")[0]
    resp, _ = request(server, "POST", "/show/1/topic-index", body={}, cookie=cookie)
    assert resp.status == 403


def test_non_admin_does_not_see_global_toggle_buttons(server, tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="letmein")
    auth.create_user("viewer")
    resp, _ = request(server, "POST", "/login", body={"username": "viewer", "password": "letmein"})
    cookie = resp.getheader("Set-Cookie").split(";")[0]
    resp, body = request(server, "GET", "/show/1", cookie=cookie)
    assert "Disable ad-stripping" not in body and "Enable ad-stripping" not in body


def test_admin_can_still_toggle_ad_stripping(server):
    cookie = login(server)
    resp, _ = request(server, "POST", "/show/1/adblock", body={}, cookie=cookie)
    assert resp.status == 303


def test_gpodder_sync_isolated_between_two_accounts(server, tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="letmein")
    auth.create_user("alice")
    # alice sets her own password so she isn't sharing admin's bootstrap token
    # (irrelevant to the isolation this test checks, just closer to real use)
    admin_auth = ("admin", "letmein")
    alice_auth = ("alice", "letmein")

    request(server, "POST", "/index.php/apps/gpoddersync/subscription_change/create",
            auth=admin_auth, json_body={"add": ["https://admin-only.example/feed"], "remove": []})
    request(server, "POST", "/index.php/apps/gpoddersync/subscription_change/create",
            auth=alice_auth, json_body={"add": ["https://alice-only.example/feed"], "remove": []})

    resp, body = request(server, "GET", "/index.php/apps/gpoddersync/subscriptions",
                         auth=admin_auth, json_body=None)
    admin_add = json.loads(body)["add"]
    resp, body = request(server, "GET", "/index.php/apps/gpoddersync/subscriptions",
                         auth=alice_auth, json_body=None)
    alice_add = json.loads(body)["add"]

    assert "https://admin-only.example/feed" in admin_add
    assert "https://alice-only.example/feed" not in admin_add
    assert "https://alice-only.example/feed" in alice_add
    assert "https://admin-only.example/feed" not in alice_add


def test_gpodder_sync_both_accounts_share_one_global_show_row(server, tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="letmein")
    auth.create_user("alice")
    for creds in (("admin", "letmein"), ("alice", "letmein")):
        request(server, "POST", "/index.php/apps/gpoddersync/subscription_change/create",
                auth=creds, json_body={"add": ["https://shared.example/feed"], "remove": []})
    conn = db.connect(tmp_path / "hark.db")
    assert conn.execute(
        "SELECT COUNT(*) FROM shows WHERE feed_url = ?", ("https://shared.example/feed",)
    ).fetchone()[0] == 1


# --- Invite links: HTTP (0.14.0) ---

def test_invite_page_renders_for_valid_token(server, tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="letmein")
    _, token = auth.create_invite("alice")
    resp, body = request(server, "GET", f"/invite/{token}")
    assert resp.status == 200
    assert "alice" in body


def test_invite_page_404s_for_unknown_token(server):
    resp, body = request(server, "GET", "/invite/not-a-real-token")
    assert resp.status == 404
    assert "invalid or has expired" in body


def test_invite_accept_sets_password_and_logs_in(server, tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="letmein")
    _, token = auth.create_invite("alice")
    resp, _ = request(server, "POST", f"/invite/{token}",
                      body={"password": "a-real-password", "password2": "a-real-password"})
    assert resp.status == 303
    assert resp.getheader("Set-Cookie", "").startswith("hark_session=")
    assert auth.verify("alice", "a-real-password") is not None


def test_invite_accept_rejects_short_password(server, tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="letmein")
    _, token = auth.create_invite("alice")
    resp, body = request(server, "POST", f"/invite/{token}",
                         body={"password": "short", "password2": "short"})
    assert resp.status == 400
    assert "too short" in body


def test_invite_accept_rejects_mismatched_passwords(server, tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="letmein")
    _, token = auth.create_invite("alice")
    resp, body = request(server, "POST", f"/invite/{token}",
                         body={"password": "a-real-password", "password2": "different-password"})
    assert resp.status == 400
    assert "do not match" in body


def test_invite_accept_cannot_be_reused(server, tmp_path):
    auth = web.Auth(tmp_path / "auth.db", admin_token="letmein")
    _, token = auth.create_invite("alice")
    request(server, "POST", f"/invite/{token}",
            body={"password": "first-password", "password2": "first-password"})
    resp, _ = request(server, "POST", f"/invite/{token}",
                      body={"password": "second-password", "password2": "second-password"})
    assert resp.status == 404


# --- /admin/users (0.14.0) ---

def test_admin_users_page_requires_admin(server, tmp_path):
    web.Auth(tmp_path / "auth.db", admin_token="letmein").create_user("viewer")
    resp, _ = request(server, "POST", "/login", body={"username": "viewer", "password": "letmein"})
    cookie = resp.getheader("Set-Cookie").split(";")[0]
    resp, _ = request(server, "GET", "/admin/users", cookie=cookie)
    assert resp.status == 403


def test_admin_users_page_lists_accounts(server, tmp_path):
    web.Auth(tmp_path / "auth.db", admin_token="letmein").create_user("alice")
    cookie = login(server)
    resp, body = request(server, "GET", "/admin/users", cookie=cookie)
    assert resp.status == 200
    assert "admin" in body and "alice" in body


def test_admin_create_invite_shows_link(server):
    cookie = login(server)
    resp, _ = request(server, "POST", "/admin/users/invite",
                      body={"username": "bob"}, cookie=cookie)
    assert resp.status == 303
    location = resp.getheader("Location")
    assert "invite_link=" in location

    resp, body = request(server, "GET", "/admin/users", cookie=cookie)
    assert "/invite/" in body


def test_admin_create_invite_requires_admin(server, tmp_path):
    web.Auth(tmp_path / "auth.db", admin_token="letmein").create_user("viewer")
    resp, _ = request(server, "POST", "/login", body={"username": "viewer", "password": "letmein"})
    cookie = resp.getheader("Set-Cookie").split(";")[0]
    resp, _ = request(server, "POST", "/admin/users/invite", body={"username": "bob"}, cookie=cookie)
    assert resp.status == 403


def test_admin_remove_user(server, tmp_path):
    web.Auth(tmp_path / "auth.db", admin_token="letmein").create_user("alice")
    cookie = login(server)
    resp, _ = request(server, "POST", "/admin/users/remove", body={"username": "alice"}, cookie=cookie)
    assert resp.status == 303
    resp, body = request(server, "GET", "/admin/users", cookie=cookie)
    assert "alice" not in body


def test_admin_cannot_remove_own_account(server):
    cookie = login(server)
    resp, body = request(server, "POST", "/admin/users/remove", body={"username": "admin"}, cookie=cookie)
    assert resp.status == 400
    assert "own account" in body


# --- Per-user show quota (0.14.0) ---

def _seed_shows(tmp_path, n, offset=0):
    conn = db.connect(tmp_path / "hark.db")
    for i in range(offset, offset + n):
        conn.execute(
            "INSERT INTO shows (query, title, feed_url) VALUES (?, ?, ?)",
            (f"q{i}", f"Show {i}", f"http://x/{i}"),
        )
    conn.commit()
    conn.close()


def test_subscribe_blocked_at_quota_for_non_admin(server, tmp_path):
    web.Auth(tmp_path / "auth.db", admin_token="letmein").create_user("alice")
    _seed_shows(tmp_path, 11, offset=100)  # 100..110 -> show ids 2..12 (id 1 is the fixture's Show A)
    resp, _ = request(server, "POST", "/login", body={"username": "alice", "password": "letmein"})
    cookie = resp.getheader("Set-Cookie").split(";")[0]

    conn = db.connect(tmp_path / "hark.db")
    show_ids = [r["id"] for r in conn.execute("SELECT id FROM shows WHERE query LIKE 'q1%'")]
    assert len(show_ids) == 11

    for sid in show_ids[:10]:
        resp, _ = request(server, "POST", f"/show/{sid}/subscribe", body={}, cookie=cookie)
        assert resp.status == 303
    resp, _ = request(server, "POST", f"/show/{show_ids[10]}/subscribe", body={}, cookie=cookie)
    assert resp.status == 303
    assert "err=quota" in resp.getheader("Location")

    resp, body = request(server, "GET", f"/show/{show_ids[10]}", cookie=cookie)
    assert "Not in your list yet." in body


def test_subscribe_admin_exempt_from_quota(server, tmp_path):
    _seed_shows(tmp_path, 11, offset=200)
    cookie = login(server)  # admin, already subscribed to Show A via the fixture
    conn = db.connect(tmp_path / "hark.db")
    show_ids = [r["id"] for r in conn.execute("SELECT id FROM shows WHERE query LIKE 'q2%'")]
    for sid in show_ids:
        resp, _ = request(server, "POST", f"/show/{sid}/subscribe", body={}, cookie=cookie)
        assert resp.status == 303
    resp, body = request(server, "GET", f"/show/{show_ids[-1]}", cookie=cookie)
    assert "In your list." in body


def test_resubscribe_at_quota_is_still_idempotent(server, tmp_path):
    # Re-adding a show already in the list must not be blocked by being AT
    # the cap — only a genuinely new addition should be.
    web.Auth(tmp_path / "auth.db", admin_token="letmein").create_user("alice")
    _seed_shows(tmp_path, 10, offset=300)
    resp, _ = request(server, "POST", "/login", body={"username": "alice", "password": "letmein"})
    cookie = resp.getheader("Set-Cookie").split(";")[0]
    conn = db.connect(tmp_path / "hark.db")
    show_ids = [r["id"] for r in conn.execute("SELECT id FROM shows WHERE query LIKE 'q3%'")]
    for sid in show_ids:
        request(server, "POST", f"/show/{sid}/subscribe", body={}, cookie=cookie)
    resp, _ = request(server, "POST", f"/show/{show_ids[0]}/subscribe", body={}, cookie=cookie)
    assert resp.status == 303
    assert "err=quota" not in resp.getheader("Location")


def test_gpodder_sync_quota_enforced_for_non_admin(server, tmp_path):
    web.Auth(tmp_path / "auth.db", admin_token="letmein").create_user("alice")
    auth = ("alice", "letmein")
    urls = [f"https://quota-test-{i}.example/feed" for i in range(15)]
    request(server, "POST", "/index.php/apps/gpoddersync/subscription_change/create",
            auth=auth, json_body={"add": urls, "remove": []})
    resp, body = request(server, "GET", "/index.php/apps/gpoddersync/subscriptions",
                         auth=auth, json_body=None)
    data = json.loads(body)
    assert len(data["add"]) == 10


def test_gpodder_sync_quota_not_enforced_for_admin(server, tmp_path):
    admin_auth = ("admin", "letmein")
    urls = [f"https://admin-quota-test-{i}.example/feed" for i in range(15)]
    request(server, "POST", "/index.php/apps/gpoddersync/subscription_change/create",
            auth=admin_auth, json_body={"add": urls, "remove": []})
    resp, body = request(server, "GET", "/index.php/apps/gpoddersync/subscriptions",
                         auth=admin_auth, json_body=None)
    data = json.loads(body)
    assert len([u for u in urls if u in data["add"]]) == 15
