"""HTTP plumbing: request routing (Handler), server construction
(make_server/serve), and the gpodder-sync request handlers — the pieces
that are genuinely about *serving HTTP*, as opposed to auth (auth.py),
DB queries (queries.py), HTML templates (templates.py), or view logic
(views.py, the App class). This module re-exports the names each of those
used to provide directly, so `web.Auth`, `web.App`, `web.esc`,
`web.topics_query`, etc. all still work — split apart 2026-07-13 once this
file alone grew past ~1900 lines mixing all of the above together.

Security model follows the influence-registry spec: the dashboard is gated
by server-side sessions carried in an HttpOnly cookie; only /login,
/logout, /invite/<token>, and /healthz are reachable unauthenticated;
fail-closed — with no admin password and no HARK_ADMIN_TOKEN the site
cannot be entered at all. Passwords are stretched (iterated salted
SHA-256) and compared in constant time; changing the password revokes
every session for that account (see auth.py).

/feed/<show_id>/<token> and /audio/<episode_id>/<token>.<ext> are also
unauthenticated, but for a different reason: a podcast app can't do the
dashboard's cookie login. They're gated instead by a per-show random token
(shows.feed_token, compared with secrets.compare_digest) embedded in the URL
— same idea as the tokened private-feed URLs most self-hosted podcast tools
use, not a second login system. The RSS itself is built by podcast_feed.py
(hark's own — adscrub's own feed-building code targets a different schema
and has no token concept); the ad-detection/cutting pipeline that populates
cut_path comes from the adscrub package (see cli.py, pyproject.toml).

Auth state lives in its own SQLite file (auth.db), NOT in hark.db — data
snapshots pushed from the pipeline replace hark.db wholesale and must never
wipe accounts or sessions.

Dependency-free by design: stdlib http.server, hashlib, secrets. Single
embedded stylesheet served from /static/style.css so the CSP can stay strict
(default-src 'self', no inline anything).
"""

from __future__ import annotations

import base64
import json
import secrets
import sqlite3
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, gpodder_server, podcast_feed
from .auth import (  # noqa: F401 — Auth/iso/utcnow/INVITE_EXPIRES_DAYS re-exported for callers
    INVITE_EXPIRES_DAYS,
    SESSION_DAYS,
    Auth,
    iso,
    parse_iso,
    utcnow,
)
from .queries import (  # noqa: F401 — re-exported for callers (cli.py, tests)
    PAGE_SIZE,
    contested_topics,
    paginate,
    rare_genre_episodes,
    related_shows,
    related_topics,
    topics_count,
    topics_query,
)
from .templates import (  # noqa: F401 — re-exported for callers (cli.py, tests)
    INVITE_INVALID_PAGE,
    INVITE_PAGE,
    LOGIN_PAGE,
    STYLE,
    conf,
    episode_cell,
    esc,
    index_status_html,
    page,
    pagination_html,
    pipeline_status_html,
    plural,
    relative_time,
    topic_pills,
    topic_table,
)
from .views import COOKIE, App  # noqa: F401 — re-exported for callers (cli.py, tests)

MAX_FORM_BYTES = 65536
# AntennaPod batches episode-action uploads at 30 per request (its own
# UPLOAD_BULK_SIZE) — plenty of headroom over that for a JSON body.
MAX_JSON_BODY_BYTES = 1_000_000


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    app: App  # set by make_server
    server_version = f"hark/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # quiet access log
        pass

    def log_error(self, format, *args):  # bypass log_message so errors still surface
        BaseHTTPRequestHandler.log_message(self, format, *args)

    # -- helpers -------------------------------------------------------------

    def _security_headers(self):
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self'; media-src *; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")

    def respond(self, status: int, body: str, content_type="text/html; charset=utf-8",
                extra_headers: dict | None = None):
        self.respond_bytes(status, body.encode(), content_type, extra_headers)

    def respond_bytes(self, status: int, data: bytes, content_type: str,
                       extra_headers: dict | None = None):
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str, extra_headers: dict | None = None):
        self.send_response(HTTPStatus.SEE_OTHER)
        self._security_headers()
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_FORM_BYTES:
            # Too large to read safely; don't try to keep the connection
            # alive with an unread tail still sitting in the socket.
            self.close_connection = True
            return {}
        raw = self.rfile.read(length).decode(errors="replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def not_found(self, user) -> None:
        return self.respond(404, page("404", "<h1>Not found</h1>", user["username"]))

    def forbidden(self, user) -> None:
        return self.respond(403, page(
            "forbidden", "<h1>Forbidden</h1><p>Admin only.</p>", user["username"]
        ))

    def db_unavailable(self, user) -> None:
        return self.respond(503, page(
            "unavailable",
            "<h1>Not ready</h1><p>The topic database hasn't been created yet — "
            "run <code>hark ingest</code> and <code>hark extract</code> "
            "(or <code>hark load</code>) first.</p>",
            user["username"],
        ))

    # -- ad-stripped podcast feed/audio (unauthenticated, token-gated) --------

    def _plain_404(self) -> None:
        self.respond_bytes(404, b"not found", "text/plain; charset=utf-8")

    def _serve_feed(self, route: str) -> None:
        # route == "/feed/<show_id>/<token>"
        parts = route.split("/")
        if len(parts) != 4:
            return self._plain_404()
        try:
            show_id = int(parts[2])
        except ValueError:
            return self._plain_404()
        token = parts[3]
        conn = self.app.db()
        try:
            show = conn.execute("SELECT * FROM shows WHERE id = ?", (show_id,)).fetchone()
            if show is None or not show["feed_token"] or not secrets.compare_digest(
                show["feed_token"], token
            ):
                return self._plain_404()
            body = podcast_feed.build_feed(conn, show, self.app.base_url)
        finally:
            conn.close()
        return self.respond_bytes(200, body, "application/rss+xml; charset=utf-8")

    def _serve_audio(self, route: str) -> None:
        # route == "/audio/<episode_id>/<token>.<ext>"
        parts = route.split("/", 3)
        if len(parts) != 4:
            return self._plain_404()
        try:
            episode_id = int(parts[2])
        except ValueError:
            return self._plain_404()
        token = parts[3].split(".", 1)[0]
        conn = self.app.db()
        try:
            row = conn.execute(
                """
                SELECT e.cut_path, s.feed_token FROM episodes e
                JOIN shows s ON s.id = e.show_id
                WHERE e.id = ?
                """,
                (episode_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or not row["feed_token"] or not secrets.compare_digest(
            row["feed_token"], token
        ):
            return self._plain_404()
        # cut_path is set by adscrub's cut.py, never user-supplied — no
        # traversal surface to guard against here, unlike a URL-derived
        # filename would need.
        cut_path = Path(row["cut_path"]) if row["cut_path"] else None
        if cut_path is None or not cut_path.is_file():
            return self._plain_404()
        return self.respond_bytes(200, cut_path.read_bytes(), "audio/mpeg")

    # -- gpodder-sync server (unauthenticated by cookie, Basic-Auth gated) ---
    # See gpodder_server.py's module docstring: implements the exact API
    # AntennaPod's own NextcloudSyncService.java calls, so pointing
    # AntennaPod's existing "Nextcloud" sync setting at hark directly works
    # with zero app changes. Same credential as the web UI's admin account
    # (Auth.verify) — a session cookie makes no sense for this client, but
    # reusing the account avoids a second credential to manage.

    def _basic_auth_user_id(self) -> int | None:
        """Multi-user (0.14.0): the gpodder-sync endpoints used to just check
        Basic Auth was valid for *some* account and scope everything to the
        global tables. Now every account's AntennaPod install syncs against
        its own subscription list/listen history, so callers need the actual
        authenticated user_id, not a bool."""
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return None
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        username, _, password = decoded.partition(":")
        return self.app.auth.verify(username, password)

    def _gpodder_unauthorized(self) -> None:
        self.respond_bytes(
            401, b"unauthorized", "text/plain; charset=utf-8",
            {"WWW-Authenticate": 'Basic realm="hark"'},
        )

    def _json_body(self, max_bytes: int = MAX_JSON_BODY_BYTES):
        """Raw JSON request body — self.form() parses url-encoded data, the
        wrong shape for what AntennaPod's sync client actually POSTs.
        Returns None on an oversized or malformed body (caller 400s)."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > max_bytes:
            self.close_connection = True
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _gpodder_get_subscriptions(self, params: dict) -> None:
        user_id = self._basic_auth_user_id()
        if user_id is None:
            return self._gpodder_unauthorized()
        since = int(params.get("since", ["0"])[0] or 0)
        conn = self.app.db()
        try:
            add, remove, ts = gpodder_server.subscription_changes_since(conn, user_id, since)
        finally:
            conn.close()
        self.respond(200, json.dumps({"add": add, "remove": remove, "timestamp": ts}),
                      "application/json")

    def _gpodder_post_subscription_changes(self) -> None:
        # Body read (and thus drained from the socket) before the auth
        # check, always — same reason do_POST's own form-drain comment
        # gives: leaving it unread on a rejected request risks the next
        # request on a kept-alive HTTP/1.1 connection misparsing those
        # leftover bytes as its own request line.
        body = self._json_body()
        user_id = self._basic_auth_user_id()
        if user_id is None:
            return self._gpodder_unauthorized()
        if not isinstance(body, dict):
            return self.respond(400, "bad request", "text/plain; charset=utf-8")
        add = [u for u in body.get("add", []) if isinstance(u, str)]
        remove = [u for u in body.get("remove", []) if isinstance(u, str)]
        is_admin = self.app.auth.is_admin(user_id)
        conn = sqlite3.connect(self.app.db_path)
        conn.row_factory = sqlite3.Row
        try:
            ts = gpodder_server.record_subscription_changes(conn, user_id, add, remove, is_admin)
        finally:
            conn.close()
        self.respond(200, json.dumps({"timestamp": ts}), "application/json")

    def _gpodder_get_episode_actions(self, params: dict) -> None:
        user_id = self._basic_auth_user_id()
        if user_id is None:
            return self._gpodder_unauthorized()
        since = int(params.get("since", ["0"])[0] or 0)
        conn = self.app.db()
        try:
            actions, ts = gpodder_server.episode_actions_since(conn, user_id, since)
        finally:
            conn.close()
        self.respond(200, json.dumps({"actions": actions, "timestamp": ts}), "application/json")

    def _gpodder_post_episode_actions(self) -> None:
        body = self._json_body()  # drain before auth check — see the sibling method's comment
        user_id = self._basic_auth_user_id()
        if user_id is None:
            return self._gpodder_unauthorized()
        if not isinstance(body, list):
            return self.respond(400, "bad request", "text/plain; charset=utf-8")
        actions = [a for a in body if isinstance(a, dict)]
        conn = sqlite3.connect(self.app.db_path)
        conn.row_factory = sqlite3.Row
        try:
            gpodder_server.record_episode_actions(conn, user_id, actions)
        finally:
            conn.close()
        self.respond(200, json.dumps({"timestamp": int(time.time())}), "application/json")

    # -- routing -------------------------------------------------------------

    def do_GET(self):
        app = self.app
        url = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(url.query)
        route = url.path.rstrip("/") or "/"

        if route == "/healthz":
            return self.respond(200, "ok", "text/plain; charset=utf-8")
        if route == "/static/style.css":
            return self.respond(200, STYLE, "text/css; charset=utf-8")
        if route == "/login":
            return self.respond(200, page("login", LOGIN_PAGE.format(err="")))
        if route.startswith("/invite/"):
            token = route.removeprefix("/invite/")
            invite = app.auth.find_by_invite_token(token)
            if invite is None:
                return self.respond(404, page("invite", INVITE_INVALID_PAGE))
            body = INVITE_PAGE.format(username=esc(invite["username"]), token=esc(token), err="")
            return self.respond(200, page("invite", body))
        if route.startswith("/feed/"):
            return self._serve_feed(route)
        if route.startswith("/audio/"):
            return self._serve_audio(route)
        if route == "/index.php/apps/gpoddersync/subscriptions":
            return self._gpodder_get_subscriptions(params)
        if route == "/index.php/apps/gpoddersync/episode_action":
            return self._gpodder_get_episode_actions(params)

        user = app.auth.session_user(self.cookie_token())
        if user is None:
            return self.redirect("/login")

        try:
            if route == "/":
                return self.respond(200, app.view_home(user))
            if route == "/topics":
                return self.respond(200, app.view_topics(user, params))
            if route == "/notable":
                return self.respond(200, app.view_notable(user))
            if route.startswith("/topic/"):
                try:
                    topic_id = int(route.rsplit("/", 1)[1])
                except ValueError:
                    return self.not_found(user)
                body = app.view_topic(user, topic_id)
                if body is None:
                    return self.not_found(user)
                return self.respond(200, body)
            if route == "/search":
                return self.respond(200, app.view_search(user, params))
            if route == "/shows":
                return self.respond(200, app.view_shows(user, params))
            if route.startswith("/show/"):
                try:
                    show_id = int(route.rsplit("/", 1)[1])
                except ValueError:
                    return self.not_found(user)
                body = app.view_show(user, show_id, params)
                if body is None:
                    return self.not_found(user)
                return self.respond(200, body)
            if route.startswith("/episode/"):
                try:
                    episode_id = int(route.rsplit("/", 1)[1])
                except ValueError:
                    return self.not_found(user)
                body = app.view_episode(user, episode_id)
                if body is None:
                    return self.not_found(user)
                return self.respond(200, body)
            if route == "/account":
                return self.respond(200, app.view_account(user))
            if route == "/admin/users":
                if not user["is_admin"]:
                    return self.forbidden(user)
                return self.respond(200, app.view_admin_users(user, params))
        except sqlite3.OperationalError:
            return self.db_unavailable(user)
        return self.not_found(user)

    def do_POST(self):
        app = self.app
        route = self.path.rstrip("/")

        # Dispatched before self.form()'s drain-the-body call below — these
        # POST a JSON body, not form-encoded data, and need to read it
        # themselves via _json_body() while the stream is still untouched.
        if route == "/index.php/apps/gpoddersync/subscription_change/create":
            return self._gpodder_post_subscription_changes()
        if route == "/index.php/apps/gpoddersync/episode_action/create":
            return self._gpodder_post_episode_actions()

        form = self.form()  # always drain the body, even on routes that ignore it

        if route == "/login":
            user_id = app.auth.verify(form.get("username", ""), form.get("password", ""))
            if user_id is None:
                time.sleep(0.4)  # blunt brute-force throttle
                body = page("login", LOGIN_PAGE.format(err='<p class="err">No.</p>'))
                return self.respond(HTTPStatus.UNAUTHORIZED, body)
            token = app.auth.create_session(user_id)
            return self.redirect("/", {"Set-Cookie": app.cookie_attrs(token, SESSION_DAYS * 86_400)})

        if route.startswith("/invite/"):
            invite_token = route.removeprefix("/invite/")
            invite = app.auth.find_by_invite_token(invite_token)
            if invite is None:
                return self.respond(404, page("invite", INVITE_INVALID_PAGE))
            pw, pw2 = form.get("password", ""), form.get("password2", "")
            err = None
            if len(pw) < 8:
                err = "Password too short (min 8)."
            elif pw != pw2:
                err = "Passwords do not match."
            if err:
                body = INVITE_PAGE.format(
                    username=esc(invite["username"]), token=esc(invite_token),
                    err=f'<p class="err">{esc(err)}</p>',
                )
                return self.respond(400, page("invite", body))
            user_id = app.auth.accept_invite(invite_token, pw)
            if user_id is None:  # expired between the GET and this POST
                return self.respond(404, page("invite", INVITE_INVALID_PAGE))
            session_token = app.auth.create_session(user_id)
            return self.redirect(
                "/", {"Set-Cookie": app.cookie_attrs(session_token, SESSION_DAYS * 86_400)}
            )

        user = app.auth.session_user(self.cookie_token())
        if user is None:
            return self.redirect("/login")

        try:
            if route == "/logout":
                app.auth.drop_session(self.cookie_token())
                return self.redirect("/login", {"Set-Cookie": app.cookie_attrs("", 0)})
            if route == "/account/password":
                pw, pw2 = form.get("password", ""), form.get("password2", "")
                if len(pw) < 8:
                    return self.respond(400, app.view_account(user, err="Password too short (min 8)."))
                if pw != pw2:
                    return self.respond(400, app.view_account(user, err="Passwords do not match."))
                app.auth.set_password(user["id"], pw)
                return self.redirect("/login", {"Set-Cookie": app.cookie_attrs("", 0)})
            if route == "/admin/users/invite":
                if not user["is_admin"]:
                    return self.forbidden(user)
                username = form.get("username", "").strip()
                if not username:
                    return self.respond(
                        400, app.view_admin_users(user, {}, msg="Username required.")
                    )
                try:
                    _, invite_token = app.auth.create_invite(
                        username, is_admin=form.get("is_admin") == "1"
                    )
                except sqlite3.IntegrityError:
                    return self.respond(
                        400, app.view_admin_users(user, {}, msg=f"{username!r} already exists.")
                    )
                link = f"{app.base_url}/invite/{invite_token}"
                return self.redirect(f"/admin/users?invite_link={urllib.parse.quote(link)}")
            if route == "/admin/users/remove":
                if not user["is_admin"]:
                    return self.forbidden(user)
                username = form.get("username", "")
                if username == user["username"]:
                    return self.respond(
                        400, app.view_admin_users(user, {}, msg="Can't remove your own account.")
                    )
                app.auth.delete_user(username)
                return self.redirect("/admin/users")
            if route.startswith("/show/") and route.endswith("/adblock"):
                if not user["is_admin"]:
                    return self.forbidden(user)
                try:
                    show_id = int(route.removeprefix("/show/").removesuffix("/adblock"))
                except ValueError:
                    return self.not_found(user)
                if app.toggle_ad_stripping(show_id) is None:
                    return self.not_found(user)
                return self.redirect(f"/show/{show_id}")
            if route.startswith("/show/") and route.endswith("/topic-index"):
                if not user["is_admin"]:
                    return self.forbidden(user)
                try:
                    show_id = int(route.removeprefix("/show/").removesuffix("/topic-index"))
                except ValueError:
                    return self.not_found(user)
                if app.toggle_topic_index(show_id) is None:
                    return self.not_found(user)
                return self.redirect(f"/show/{show_id}")
            if route.startswith("/show/") and route.endswith("/subscribe"):
                try:
                    show_id = int(route.removeprefix("/show/").removesuffix("/subscribe"))
                except ValueError:
                    return self.not_found(user)
                result = app.subscribe(user["id"], show_id)
                if result is None:
                    return self.not_found(user)
                if result is False:
                    return self.redirect(f"/show/{show_id}?err=quota")
                return self.redirect(f"/show/{show_id}")
            if route.startswith("/show/") and route.endswith("/unsubscribe"):
                try:
                    show_id = int(route.removeprefix("/show/").removesuffix("/unsubscribe"))
                except ValueError:
                    return self.not_found(user)
                app.unsubscribe(user["id"], show_id)
                return self.redirect(f"/show/{show_id}")
        except sqlite3.OperationalError:
            return self.db_unavailable(user)
        return self.not_found(user)


def make_server(db_path: str | Path, auth_path: str | Path, bind: str = "0.0.0.0:8710",
                admin_token: str | None = None, cookie_secure: bool = False,
                base_url: str = "http://localhost:8710") -> ThreadingHTTPServer:
    auth = Auth(auth_path, admin_token=admin_token)
    app = App(db_path, auth, cookie_secure=cookie_secure, base_url=base_url)
    host, _, port = bind.rpartition(":")
    try:
        port_num = int(port)
    except ValueError:
        raise SystemExit(f"invalid --bind {bind!r}: expected host:port or :port")
    handler = type("BoundHandler", (Handler,), {"app": app})
    return ThreadingHTTPServer((host or "0.0.0.0", port_num), handler)


def serve(db_path: str | Path, auth_path: str | Path, bind: str, admin_token: str | None,
          cookie_secure: bool, base_url: str = "http://localhost:8710") -> None:
    if "localhost" in base_url or "127.0.0.1" in base_url:
        print(f"warning: --base-url is {base_url!r} — a podcast player running "
              f"anywhere but this exact machine won't be able to reach cut audio "
              f"links embedded in the generated feed. Set --base-url/$HARK_BASE_URL "
              f"to this host's actual reachable address.")
    server = make_server(db_path, auth_path, bind, admin_token, cookie_secure, base_url)
    print(f"hark web on http://{bind} (db={db_path}, auth={auth_path})")
    if not admin_token:
        print("note: no HARK_ADMIN_TOKEN set — login is impossible until a "
              "password exists in the auth db (fail-closed)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
