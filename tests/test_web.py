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
    for path in ("/", "/topics", "/topic/1", "/search", "/shows", "/account"):
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

    resp, body = request(server, "GET", "/topic/1", cookie=cookie)
    assert resp.status == 200
    assert "Somerton Man" in body and "Q923144" in body and "Show A" in body

    resp, body = request(server, "GET", "/search?q=somerton", cookie=cookie)
    assert resp.status == 200 and "Somerton Man" in body

    resp, body = request(server, "GET", "/topics?genre=mystery", cookie=cookie)
    assert resp.status == 200 and "Somerton Man" in body


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
