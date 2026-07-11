"""Token-gated /feed and /audio routes: real HTTP against a server on an
ephemeral port, same pattern as test_web.py. Kept in its own file since these
routes are deliberately unauthenticated (no cookie login — see web.py's
module docstring) and that's worth testing in isolation from the dashboard's
login-wall behavior.
"""

import http.client
import threading

import pytest

from hark import db, web

TOKEN = "test-token-abc123"


@pytest.fixture
def server(tmp_path):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute(
        "INSERT INTO shows (query, title, feed_url, description, image_url, feed_token)"
        " VALUES ('q', 'Show A', 'http://original/feed', 'A show', 'http://original/art.png', ?)",
        (TOKEN,),
    )
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, description, pubdate, audio_url)"
        " VALUES (1, 'ep-1', 'Ep 1', 'desc', '2026-01-01T00:00:00Z', 'http://original/ep1.mp3')"
    )
    conn.commit()
    conn.close()

    cut_dir = tmp_path / "cut"
    cut_dir.mkdir()
    cut_path = cut_dir / "2.mp3"
    cut_path.write_bytes(b"cut-bytes")
    conn = db.connect(tmp_path / "hark.db")
    conn.execute(
        "INSERT INTO episodes (show_id, guid, title, audio_url, cut_path)"
        " VALUES (1, 'ep-2', 'Ep 2', 'http://original/ep2.mp3', ?)",
        (str(cut_path),),
    )
    conn.commit()
    conn.close()

    srv = web.make_server(
        tmp_path / "hark.db", tmp_path / "auth.db", bind="127.0.0.1:0",
        admin_token="letmein", base_url="http://myhost:8710",
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()


def request(srv, path):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


def test_feed_route_no_login_required(server):
    resp, data = request(server, f"/feed/1/{TOKEN}")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "application/rss+xml; charset=utf-8"
    assert b"Show A" in data


def test_feed_route_wrong_token_404s(server):
    resp, _ = request(server, "/feed/1/wrong-token")
    assert resp.status == 404


def test_feed_route_unknown_show_404s(server):
    resp, _ = request(server, f"/feed/999/{TOKEN}")
    assert resp.status == 404


def test_feed_route_passthrough_for_uncut_episode(server):
    _resp, data = request(server, f"/feed/1/{TOKEN}")
    assert b'url="http://original/ep1.mp3"' in data


def test_feed_route_points_cut_episode_at_local_audio(server):
    _resp, data = request(server, f"/feed/1/{TOKEN}")
    assert f'url="http://myhost:8710/audio/2/{TOKEN}.mp3"'.encode() in data


def test_audio_route_serves_cut_file_with_correct_token(server):
    resp, data = request(server, f"/audio/2/{TOKEN}.mp3")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "audio/mpeg"
    assert data == b"cut-bytes"


def test_audio_route_wrong_token_404s(server):
    resp, _ = request(server, "/audio/2/wrong-token.mp3")
    assert resp.status == 404


def test_audio_route_uncut_episode_404s(server):
    # ep-1 has no cut_path — nothing to serve locally for it
    resp, _ = request(server, f"/audio/1/{TOKEN}.mp3")
    assert resp.status == 404


def test_audio_route_unknown_episode_404s(server):
    resp, _ = request(server, f"/audio/999/{TOKEN}.mp3")
    assert resp.status == 404


def test_unknown_route_falls_through_to_dashboard_login_wall(server):
    # confirms /feed and /audio prefix checks don't swallow unrelated routes —
    # an unauthenticated request to anything else still hits the normal
    # dashboard dispatcher, which redirects to /login (not a 404, since that
    # dispatcher's own not_found() is gated behind the session check too).
    resp, _ = request(server, "/nonsense")
    assert resp.status == 303
    assert resp.getheader("Location") == "/login"


class _FakeServer:
    def serve_forever(self):
        pass


def test_serve_warns_when_base_url_is_localhost(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(web, "make_server", lambda *a, **k: _FakeServer())
    web.serve(
        tmp_path / "hark.db", tmp_path / "auth.db", "127.0.0.1:0", None, False,
        base_url="http://localhost:8710",
    )
    assert "warning:" in capsys.readouterr().out.lower()


def test_serve_no_warning_for_a_configured_hostname(tmp_path, capsys, monkeypatch):
    # note: matching the exact "warning:" prefix (not a bare "warning"
    # substring) since pytest's tmp_path embeds the test's own function name,
    # and a name containing "warning" would otherwise leak into the printed
    # db path and self-sabotage this assertion.
    monkeypatch.setattr(web, "make_server", lambda *a, **k: _FakeServer())
    web.serve(
        tmp_path / "hark.db", tmp_path / "auth.db", "127.0.0.1:0", None, False,
        base_url="http://truenas.local:8710",
    )
    assert "warning:" not in capsys.readouterr().out.lower()
