"""Web frontend tests: real HTTP against a server on an ephemeral port.

Password stretching is tuned down via monkeypatching PW_ITERS so the suite
stays fast; the logic under test is identical.
"""

import http.client
import threading
import urllib.parse

import pytest

from hark import db, web


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
    conn.commit()
    conn.close()

    srv = web.make_server(tmp_path / "hark.db", tmp_path / "auth.db",
                          bind="127.0.0.1:0", admin_token="letmein")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()


def request(srv, method, path, body=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    if body is not None:
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
    for path in ("/", "/topics", "/topic/1", "/search", "/shows", "/show/1", "/account"):
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


def test_login_with_admin_token_and_browse(server):
    cookie = login(server)
    assert cookie.startswith("hark_session=")

    resp, body = request(server, "GET", "/", cookie=cookie)
    assert resp.status == 200 and "Who covered it?" in body
    assert "Fully indexed" in body  # fixture's one episode is already extracted
    assert 'href="/topics?genre=mystery">mystery (1)' in body  # genre breakdown
    assert "Recently indexed" in body and "Case 1: Somerton" in body

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
        assert "1 episode(s) not yet indexed" in body
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
