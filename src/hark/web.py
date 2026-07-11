"""Web frontend: the cross-show topic index behind a login wall, plus the
ad-stripped podcast feed/audio routes (unauthenticated, token-gated).

Security model follows the influence-registry spec: the dashboard is gated
by server-side sessions carried in an HttpOnly cookie; only /login, /logout
and /healthz are reachable unauthenticated; fail-closed — with no admin
password and no HARK_ADMIN_TOKEN the site cannot be entered at all.
Passwords are stretched (iterated salted SHA-256) and compared in constant
time; changing the password revokes every session.

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

import contextlib
import hashlib
import html
import os
import secrets
import sqlite3
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, claims, podcast_feed
from .extract import GENRES as GENRES_FILTER

PW_ITERS = 120_000
SESSION_DAYS = 30
COOKIE = "hark_session"
MAX_FORM_BYTES = 65536

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    salt          TEXT,
    password_hash TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
"""


def stretch(salt: str, password: str) -> str:
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    for _ in range(PW_ITERS):
        h = hashlib.sha256(h.encode()).hexdigest()
    return h


def constant_eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def relative_time(dt: datetime) -> str:
    secs = (utcnow() - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


class Auth:
    """Accounts and sessions in their own database file."""

    def __init__(self, path: str | Path, admin_token: str | None, admin_user: str = "admin"):
        self.path = str(path)
        self.admin_token = admin_token or None
        with contextlib.closing(self._connect()) as conn:
            conn.executescript(AUTH_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO users (username) VALUES (?)", (admin_user,))
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def verify(self, username: str, password: str) -> int | None:
        """Return user id on success. Fail-closed: an account with no stored
        password only accepts the bootstrap admin token, and if that is unset
        nothing is accepted."""
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, salt, password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
            if row is None:
                stretch("timing-pad", password)  # equalise timing for unknown users
                return None
            if row["password_hash"]:
                if constant_eq(stretch(row["salt"], password), row["password_hash"]):
                    return row["id"]
                return None
            if self.admin_token and constant_eq(password, self.admin_token):
                return row["id"]
            return None

    def create_session(self, user_id: int) -> str:
        token = secrets.token_hex(32)
        with contextlib.closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
                (token, user_id, iso(utcnow() + timedelta(days=SESSION_DAYS))),
            )
            conn.commit()
        return token

    def session_user(self, token: str | None) -> sqlite3.Row | None:
        if not token:
            return None
        with contextlib.closing(self._connect()) as conn:
            return conn.execute(
                """
                SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token = ? AND s.expires_at > ?
                """,
                (token, iso(utcnow())),
            ).fetchone()

    def drop_session(self, token: str | None) -> None:
        if not token:
            return
        with contextlib.closing(self._connect()) as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()

    def set_password(self, user_id: int, password: str) -> None:
        """Set a new password and revoke every session (all devices log out)."""
        salt = secrets.token_hex(16)
        with contextlib.closing(self._connect()) as conn:
            conn.execute(
                "UPDATE users SET salt = ?, password_hash = ? WHERE id = ?",
                (salt, stretch(salt, password), user_id),
            )
            conn.execute("DELETE FROM sessions")
            conn.commit()


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

STYLE = """
:root { --bg:#101418; --panel:#1a2027; --ink:#e6e1d6; --dim:#8b9299; --acc:#d9a441; --line:#2a323b; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 Georgia, 'Times New Roman', serif; }
a { color:var(--acc); text-decoration:none; } a:hover { text-decoration:underline; }
header { border-bottom:1px solid var(--line); padding:0.8rem 1.2rem; display:flex; gap:1.2rem; align-items:baseline; flex-wrap:wrap; }
header .brand { font-size:1.25rem; letter-spacing:0.12em; color:var(--acc); }
header nav { display:flex; gap:1rem; } header .spacer { flex:1; }
main { max-width: 62rem; margin: 1.4rem auto; padding: 0 1.2rem; }
h1 { font-size:1.4rem; font-weight:normal; border-bottom:1px solid var(--line); padding-bottom:0.4rem; }
h2 { font-size:1.1rem; color:var(--dim); font-weight:normal; }
table { border-collapse:collapse; width:100%; }
td, th { padding:0.35rem 0.6rem 0.35rem 0; text-align:left; vertical-align:top; border-bottom:1px solid var(--line); }
th { color:var(--dim); font-weight:normal; font-size:0.85rem; text-transform:uppercase; letter-spacing:0.08em; }
.dim { color:var(--dim); font-size:0.9rem; } .num { text-align:right; }
.pill { display:inline-block; border:1px solid var(--line); border-radius:9px; padding:0 0.5rem; margin:0 0.2rem 0.2rem 0; font-size:0.78rem; color:var(--dim); }
form.search { display:flex; gap:0.5rem; margin:1rem 0; }
input[type=text], input[type=password] { background:var(--panel); border:1px solid var(--line); color:var(--ink); padding:0.45rem 0.6rem; font:inherit; flex:1; }
button { background:var(--acc); border:0; color:#151007; padding:0.45rem 1rem; font:inherit; cursor:pointer; }
button.ghost { background:transparent; border:1px solid var(--line); color:var(--ink); }
.cards { display:grid; grid-template-columns:repeat(auto-fit, minmax(10rem,1fr)); gap:0.8rem; margin:1.2rem 0; }
.card { background:var(--panel); border:1px solid var(--line); padding:0.8rem 1rem; }
.card .big { font-size:1.6rem; color:var(--acc); }
.login-box { max-width:22rem; margin:14vh auto; background:var(--panel); border:1px solid var(--line); padding:1.6rem; }
.login-box input { width:100%; margin-bottom:0.8rem; }
.login-box h1 { margin-top:0; }
.account-actions { display:flex; flex-direction:column; gap:1rem; align-items:flex-start; }
.account-actions .login-box { margin:0; }
.err { color:#e07a5f; }
.qid { font-size:0.78rem; color:var(--dim); }
.status { border:1px solid var(--line); border-left:3px solid var(--dim); background:var(--panel); padding:0.6rem 1rem; margin:1rem 0; font-size:0.9rem; }
.status.active { border-left-color:var(--acc); }
.status p { margin:0.2rem 0; }
.pending { color:var(--acc); }
ul.claims { margin:0.2rem 0 1rem 1.2rem; padding:0; }
ul.claims li { margin-bottom:0.3rem; }
code { background:var(--panel); border:1px solid var(--line); padding:0 0.3rem; font-size:0.9rem; }
"""

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title} — hark</title>
<link rel="stylesheet" href="/static/style.css">
</head><body>
{header}
<main>
{body}
</main>
</body></html>"""

HEADER = """<header>
<a class="brand" href="/">HARK</a>
<nav><a href="/topics">topics</a> <a href="/shows">shows</a> <a href="/search">search</a></nav>
<span class="spacer"></span>
<nav><span class="dim">{user}</span> <a href="/account">account</a></nav>
</header>"""


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def page(title: str, body: str, user: str | None = None) -> str:
    header = HEADER.format(user=esc(user)) if user else ""
    return PAGE.format(title=esc(title), header=header, body=body)


# ---------------------------------------------------------------------------
# App: routing + views over hark.db
# ---------------------------------------------------------------------------

class App:
    def __init__(
        self, db_path: str | Path, auth: Auth, cookie_secure: bool = False,
        base_url: str = "http://localhost:8710",
    ):
        self.db_path = str(db_path)
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.cookie_secure = cookie_secure

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def toggle_ad_stripping(self, show_id: int) -> bool | None:
        """Flip a show's ad_stripping_enabled flag. Returns the new state, or
        None if the show doesn't exist.

        Deliberately the only write path from web.py into hark.db (every
        other mutation here lives in auth.db instead — see module docstring:
        data snapshots pushed from the pipeline replace hark.db wholesale).
        A toggle set here will be lost on the next such re-sync unless the
        source-side hark.db (wherever the pipeline actually runs) is updated
        to match — same caveat as any other hark.db value set outside the
        pipeline host.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT ad_stripping_enabled FROM shows WHERE id = ?", (show_id,)
            ).fetchone()
            if row is None:
                return None
            new_state = 0 if row["ad_stripping_enabled"] else 1
            conn.execute(
                "UPDATE shows SET ad_stripping_enabled = ? WHERE id = ?", (new_state, show_id)
            )
            conn.commit()
            return bool(new_state)
        finally:
            conn.close()

    def cookie_attrs(self, token: str, max_age: int) -> str:
        secure = "; Secure" if self.cookie_secure else ""
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax{secure}; Max-Age={max_age}"

    # -- views ---------------------------------------------------------------

    def view_home(self, user) -> str:
        conn = self.db()
        try:
            shows = conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
            episodes = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            extracted = conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE extracted_at IS NOT NULL"
            ).fetchone()[0]
            topics = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
            canonicalized = conn.execute(
                "SELECT COUNT(*) FROM topics WHERE wikidata_id IS NOT NULL"
            ).fetchone()[0]
            last_extracted_at = conn.execute(
                "SELECT MAX(extracted_at) FROM episodes WHERE extracted_at IS NOT NULL"
            ).fetchone()[0]
            cross = conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT et.topic_id FROM episode_topics et
                    JOIN episodes e ON e.id = et.episode_id
                    GROUP BY et.topic_id HAVING COUNT(DISTINCT e.show_id) > 1)
                """
            ).fetchone()[0]
            rows = conn.execute(*topics_query(limit=15)).fetchall()
            genre_counts = conn.execute(
                """
                SELECT genre, COUNT(DISTINCT topic_id) AS n
                FROM topic_genres GROUP BY genre ORDER BY n DESC
                """
            ).fetchall()
            recent = conn.execute(
                """
                SELECT e.id, e.title, e.extracted_at, s.id AS show_id,
                       COALESCE(s.title, s.query) AS show
                FROM episodes e JOIN shows s ON s.id = e.show_id
                WHERE e.extracted_at IS NOT NULL
                ORDER BY e.extracted_at DESC LIMIT 8
                """
            ).fetchall()
        finally:
            conn.close()
        cards = f"""
        <div class="cards">
        <div class="card"><div class="big">{topics}</div>topics</div>
        <div class="card"><div class="big">{cross}</div>covered by 2+ shows</div>
        <div class="card"><div class="big">{extracted}/{episodes}</div>episodes indexed</div>
        <div class="card"><div class="big">{shows}</div>shows</div>
        </div>"""
        status = index_status_html(episodes - extracted, topics - canonicalized, last_extracted_at)
        pills = " ".join(
            f'<a class="pill" href="/topics?genre={esc(r["genre"])}">{esc(r["genre"])} ({r["n"]})</a>'
            for r in genre_counts
        )
        recent_html = "".join(
            f"<tr><td class='dim'>{relative_time(parse_iso(r['extracted_at']))}</td>"
            f"<td><a href='/show/{r['show_id']}'>{esc(r['show'])}</a></td>"
            f"<td><a href='/episode/{r['id']}'>{esc(r['title'])}</a></td></tr>"
            for r in recent
        ) or '<tr><td class="dim" colspan="3">Nothing indexed yet.</td></tr>'
        body = (
            "<h1>Who covered it?</h1>" + status +
            '<form class="search" action="/search" method="get">'
            '<input type="text" name="q" placeholder="Dyatlov, Somerton, Titanic&hellip;" autofocus>'
            "<button>Search</button></form>" + cards +
            (f"<p>{pills}</p>" if pills else "") +
            "<h2>Most covered across shows</h2>" + topic_table(rows) +
            (f'<p><a href="/topics">view all {topics} topics &raquo;</a></p>' if topics > len(rows) else "") +
            "<h2>Recently indexed</h2>"
            f"<table><tr><th>when</th><th>show</th><th>episode</th></tr>{recent_html}</table>"
        )
        return page("index", body, user["username"])

    def view_topics(self, user, params) -> str:
        genre = params.get("genre", [""])[0]
        if genre not in GENRES_FILTER:
            genre = ""
        page_num = paginate(params)
        conn = self.db()
        try:
            total = conn.execute(*topics_count(genre=genre)).fetchone()[0]
            rows = conn.execute(
                *topics_query(genre=genre, limit=PAGE_SIZE, offset=(page_num - 1) * PAGE_SIZE)
            ).fetchall()
        finally:
            conn.close()
        pills = " ".join(
            f'<a class="pill" href="/topics?genre={g}">{g}</a>' for g in GENRES_FILTER
        )
        title = f"topics — {genre}" if genre else "topics"
        query = {"genre": genre} if genre else {}
        pager = pagination_html("/topics", query, page_num, total, "topics")
        body = f"<h1>{esc(title)}</h1><p>{pills}</p>" + topic_table(rows) + pager
        return page(title, body, user["username"])

    def view_topic(self, user, topic_id: int) -> str | None:
        conn = self.db()
        try:
            topic = conn.execute(
                "SELECT id, label, wikidata_id FROM topics WHERE id = ?", (topic_id,)
            ).fetchone()
            if topic is None:
                return None
            genres = [r["genre"] for r in conn.execute(
                "SELECT genre FROM topic_genres WHERE topic_id = ? ORDER BY genre", (topic_id,))]
            episodes = conn.execute(
                """
                SELECT e.id AS id, s.id AS show_id, COALESCE(s.title, s.query) AS show,
                       e.title, e.pubdate, e.audio_url, et.confidence
                FROM episode_topics et
                JOIN episodes e ON e.id = et.episode_id
                JOIN shows s ON s.id = e.show_id
                WHERE et.topic_id = ?
                ORDER BY show, e.pubdate
                """,
                (topic_id,),
            ).fetchall()
        finally:
            conn.close()
        qid = ""
        if topic["wikidata_id"]:
            qid = (f' <a class="qid" href="https://www.wikidata.org/wiki/'
                   f'{esc(topic["wikidata_id"])}" rel="noreferrer">{esc(topic["wikidata_id"])}</a>')
        pills = " ".join(f'<a class="pill" href="/topics?genre={esc(g)}">{esc(g)}</a>' for g in genres)
        shows = sorted({(r["show_id"], r["show"]) for r in episodes}, key=lambda s: s[1])
        show_pills = " ".join(f'<a class="pill" href="/show/{sid}">{esc(name)}</a>' for sid, name in shows)
        rows_html = "".join(
            f"<tr><td><a href='/show/{r['show_id']}'>{esc(r['show'])}</a></td><td>{episode_cell(r)}</td>"
            f"<td class='dim'>{esc((r['pubdate'] or '')[:10])}</td>"
            f"<td class='num dim'>{conf(r['confidence'])}</td></tr>"
            for r in episodes
        )
        body = (
            f"<h1>{esc(topic['label'])}{qid}</h1><p>{pills}</p>"
            f"<h2>covered by {plural(len(shows), 'show')}, {plural(len(episodes), 'episode')}</h2>"
            f"<p>{show_pills}</p>"
            f'<table><tr><th>show</th><th>episode</th><th>date</th>'
            f'<th title="extractor\'s confidence this episode is really about this topic">conf</th></tr>'
            f"{rows_html}</table>"
        )
        return page(topic["label"], body, user["username"])

    def view_search(self, user, params) -> str:
        q = params.get("q", [""])[0].strip()
        page_num = paginate(params)
        topics, episodes, topic_total, episode_total = [], [], 0, 0
        if q:
            like = f"%{q}%"
            conn = self.db()
            try:
                topic_total = conn.execute(*topics_count(q=q)).fetchone()[0]
                topics = conn.execute(
                    *topics_query(q=q, limit=PAGE_SIZE, offset=(page_num - 1) * PAGE_SIZE)
                ).fetchall()
                episode_total = conn.execute(
                    """
                    SELECT COUNT(*) FROM episodes e WHERE e.title LIKE ? COLLATE NOCASE
                    """,
                    (like,),
                ).fetchone()[0]
                episodes = conn.execute(
                    """
                    SELECT e.id, e.title, e.pubdate, s.id AS show_id,
                           COALESCE(s.title, s.query) AS show
                    FROM episodes e JOIN shows s ON s.id = e.show_id
                    WHERE e.title LIKE ? COLLATE NOCASE
                    ORDER BY e.pubdate DESC LIMIT 50
                    """,
                    (like,),
                ).fetchall()
            finally:
                conn.close()
        body = (
            "<h1>search</h1>"
            '<form class="search" action="/search" method="get">'
            f'<input type="text" name="q" value="{esc(q)}" autofocus><button>Search</button></form>'
        )
        if q:
            topics_pager = pagination_html("/search", {"q": q}, page_num, topic_total, "topics")
            no_match = f"No topics match “{q}”."
            body += (f"<h2>{plural(topic_total, 'topic')}</h2>"
                     + topic_table(topics, empty=no_match) + topics_pager)
            if episodes:
                eps = "".join(
                    f"<tr><td><a href='/show/{r['show_id']}'>{esc(r['show'])}</a></td>"
                    f"<td><a href='/episode/{r['id']}'>{esc(r['title'])}</a></td>"
                    f"<td class='dim'>{esc((r['pubdate'] or '')[:10])}</td></tr>"
                    for r in episodes
                )
                eps_table = f"<table><tr><th>show</th><th>episode</th><th>date</th></tr>{eps}</table>"
            else:
                eps_table = f'<p class="dim">No episode titles match “{q}”.</p>'
            note = (f'<p class="dim">showing the 50 most recent of {episode_total} — '
                    f"narrow your search to see the rest.</p>") if episode_total > 50 else ""
            match_word = "match" if episode_total == 1 else "matches"
            body += f"<h2>{episode_total} episode title {match_word}</h2>{eps_table}{note}"
        return page("search", body, user["username"])

    def view_shows(self, user) -> str:
        conn = self.db()
        try:
            rows = conn.execute(
                """
                SELECT s.id, COALESCE(s.title, s.query) AS name, COUNT(e.id) AS episodes,
                       COALESCE(SUM(e.extracted_at IS NOT NULL), 0) AS extracted,
                       MAX(e.pubdate) AS latest
                FROM shows s LEFT JOIN episodes e ON e.show_id = s.id
                GROUP BY s.id ORDER BY name
                """
            ).fetchall()
        finally:
            conn.close()
        table = "".join(
            f"<tr><td><a href='/show/{r['id']}'>{esc(r['name'])}</a></td>"
            f"<td class='num'>{r['episodes']}</td>"
            f"<td class='num{'' if r['extracted'] == r['episodes'] else ' pending'}'>{r['extracted']}</td>"
            f"<td class='dim'>{esc((r['latest'] or '')[:10])}</td></tr>"
            for r in rows
        )
        body = ("<h1>shows</h1><table><tr><th>show</th><th>episodes</th>"
                f"<th>indexed</th><th>latest</th></tr>{table}</table>")
        return page("shows", body, user["username"])

    def view_show(self, user, show_id: int, params) -> str | None:
        page_num = paginate(params)
        conn = self.db()
        try:
            show = conn.execute(
                """
                SELECT id, COALESCE(title, query) AS name, feed_token, ad_stripping_enabled
                FROM shows WHERE id = ?
                """,
                (show_id,),
            ).fetchone()
            if show is None:
                return None
            total_episodes = conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE show_id = ?", (show_id,)
            ).fetchone()[0]
            episodes = conn.execute(
                """
                SELECT id, title, pubdate, audio_url, extracted_at
                FROM episodes WHERE show_id = ? ORDER BY pubdate DESC LIMIT ? OFFSET ?
                """,
                (show_id, PAGE_SIZE, (page_num - 1) * PAGE_SIZE),
            ).fetchall()
            episode_ids = [r["id"] for r in episodes]
            topic_rows = []
            if episode_ids:
                placeholders = ",".join("?" * len(episode_ids))
                topic_rows = conn.execute(
                    f"""
                    SELECT et.episode_id, t.id AS topic_id, t.label
                    FROM episode_topics et
                    JOIN topics t ON t.id = et.topic_id
                    WHERE et.episode_id IN ({placeholders})
                    ORDER BY t.label
                    """,
                    episode_ids,
                ).fetchall()
            topic_count = conn.execute(
                """
                SELECT COUNT(DISTINCT et.topic_id) FROM episode_topics et
                JOIN episodes e ON e.id = et.episode_id WHERE e.show_id = ?
                """,
                (show_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        topics_by_episode: dict[int, list] = {}
        for r in topic_rows:
            topics_by_episode.setdefault(r["episode_id"], []).append(r)
        rows_html = "".join(
            f"<tr><td>{episode_cell(r)}</td>"
            f"<td class='dim'>{esc((r['pubdate'] or '')[:10])}</td>"
            f"<td>{topic_pills(topics_by_episode.get(r['id'], []), r['extracted_at'])}</td></tr>"
            for r in episodes
        )
        pager = pagination_html(f"/show/{show_id}", {}, page_num, total_episodes, "episodes")
        feed_url = f"{self.base_url}/feed/{show['id']}/{show['feed_token']}"
        enabled = bool(show["ad_stripping_enabled"])
        toggle_label = "Disable ad-stripping" if enabled else "Enable ad-stripping"
        adblock_section = (
            '<div class="status">'
            f'<p>Ad-stripped feed URL — subscribe to this in AntennaPod instead of the '
            f'original: <code>{esc(feed_url)}</code></p>'
            f'<p>Ad-stripping pipeline for this show: '
            f'<strong>{"enabled" if enabled else "disabled"}</strong> — episodes only get '
            f'transcribed/scanned/cut while enabled (existing cut episodes stay cut either way).</p>'
            f'<form method="post" action="/show/{show_id}/adblock">'
            f'<button class="ghost">{toggle_label}</button></form>'
            "</div>"
        )
        body = (
            f"<h1>{esc(show['name'])}</h1>"
            f"<h2>{plural(total_episodes, 'episode')}, {plural(topic_count, 'topic')} covered</h2>"
            f"{adblock_section}"
            f"<table><tr><th>episode</th><th>date</th><th>topics</th></tr>{rows_html}</table>{pager}"
        )
        return page(show["name"], body, user["username"])

    def view_episode(self, user, episode_id: int) -> str | None:
        conn = self.db()
        try:
            episode = conn.execute(
                """
                SELECT e.id, e.title, e.pubdate, e.audio_url, e.transcript_path,
                       e.extracted_at, s.id AS show_id, COALESCE(s.title, s.query) AS show
                FROM episodes e JOIN shows s ON s.id = e.show_id
                WHERE e.id = ?
                """,
                (episode_id,),
            ).fetchone()
            if episode is None:
                return None
            topics = conn.execute(
                """
                SELECT t.id, t.label, et.confidence
                FROM episode_topics et JOIN topics t ON t.id = et.topic_id
                WHERE et.episode_id = ? ORDER BY t.label
                """,
                (episode_id,),
            ).fetchall()
            topic_sections = []
            for t in topics:
                comparison = claims.get_comparison(conn, t["id"])
                shows_transcribed = conn.execute(
                    """
                    SELECT COUNT(DISTINCT e2.show_id) FROM episode_topics et2
                    JOIN episodes e2 ON e2.id = et2.episode_id
                    WHERE et2.topic_id = ? AND e2.transcript_path IS NOT NULL
                    """,
                    (t["id"],),
                ).fetchone()[0]
                topic_sections.append((t, comparison, shows_transcribed))
        finally:
            conn.close()

        pills = " ".join(
            f'<a class="pill" href="/topic/{t["id"]}">{esc(t["label"])}</a>' for t, _, _ in topic_sections
        )
        url = episode["audio_url"] or ""
        play = ""
        if urllib.parse.urlsplit(url).scheme in ("http", "https"):
            play = f' <a class="qid" href="{esc(url)}" rel="noreferrer">▶ play</a>'
        body = (
            f"<h1>{esc(episode['title'])}</h1>"
            f"<h2><a href='/show/{episode['show_id']}'>{esc(episode['show'])}</a>"
            f" &middot; {esc((episode['pubdate'] or '')[:10])}{play}</h2>"
            + (f"<p>{pills}</p>" if pills else "")
        )
        if not topic_sections:
            note = "not yet indexed" if not episode["extracted_at"] else "no subject identified"
            body += f'<p class="dim">{note}</p>'
        for topic, comparison, shows_transcribed in topic_sections:
            body += f"<h2>{esc(topic['label'])} — what each show said</h2>"
            if comparison is not None:
                shared_html = "".join(f"<li>{esc(c)}</li>" for c in comparison.shared)
                body += (
                    "<p>Claims shared across shows:</p>"
                    f"<ul class='claims'>{shared_html}</ul>" if comparison.shared
                    else '<p class="dim">No claims judged shared across shows.</p>'
                )
                for show, own_claims in comparison.unique_by_show.items():
                    if not own_claims:
                        continue
                    label = f"{esc(show)} (this episode)" if show == episode["show"] else esc(show)
                    items = "".join(f"<li>{esc(c)}</li>" for c in own_claims)
                    body += f"<p>Unique to {label}:</p><ul class='claims'>{items}</ul>"
                if not comparison.unique_by_show:
                    body += '<p class="dim">No claims judged unique to a single show.</p>'
            elif episode["transcript_path"] is None:
                body += '<p class="dim">This episode hasn’t been transcribed yet.</p>'
            elif shows_transcribed >= 2:
                body += ('<p class="dim">Transcribed by 2+ shows but not compared yet '
                         '(<code>hark compare</code>).</p>')
            else:
                body += '<p class="dim">Only this show has covered this topic so far.</p>'
        return page(episode["title"], body, user["username"])

    def view_account(self, user, msg: str = "", err: str = "") -> str:
        note = f'<p class="err">{esc(err)}</p>' if err else (f"<p>{esc(msg)}</p>" if msg else "")
        body = f"""<h1>account — {esc(user['username'])}</h1>{note}
<div class="account-actions">
<form method="post" action="/account/password" class="login-box">
<label>New password</label><input type="password" name="password" minlength="8" required>
<label>Repeat</label><input type="password" name="password2" minlength="8" required>
<button>Change password</button>
<p class="dim">Changing the password signs out every session.</p>
</form>
<form method="post" action="/logout"><button class="ghost">Log out</button></form>
</div>"""
        return page("account", body, user["username"])


def _topics_filter(genre: str, q: str) -> tuple[str, list]:
    if genre:
        return " WHERE t.id IN (SELECT topic_id FROM topic_genres WHERE genre = ?)", [genre]
    if q:
        return " WHERE (t.label LIKE ? COLLATE NOCASE OR t.wikidata_id = ?)", [f"%{q}%", q]
    return "", []


def topics_query(genre: str = "", q: str = "", limit: int | None = None,
                  offset: int = 0) -> tuple[str, tuple]:
    """Build the shared topic-listing query: base coverage stats, optionally
    filtered by genre or a label/QID search term, optionally paginated."""
    sql = """
        SELECT t.id, t.label, t.wikidata_id,
               COUNT(DISTINCT et.episode_id) AS episodes,
               COUNT(DISTINCT e.show_id) AS shows,
               COALESCE(GROUP_CONCAT(DISTINCT tg.genre), '') AS genres
        FROM topics t
        JOIN episode_topics et ON et.topic_id = t.id
        JOIN episodes e ON e.id = et.episode_id
        LEFT JOIN topic_genres tg ON tg.topic_id = t.id
    """
    where, params = _topics_filter(genre, q)
    sql += where + " GROUP BY t.id ORDER BY shows DESC, episodes DESC, t.label"
    if limit:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    return sql, tuple(params)


def topics_count(genre: str = "", q: str = "") -> tuple[str, tuple]:
    """Total topics matching the same filter as topics_query, for pagination.
    The filter only ever references columns on `t`, so no join is needed —
    unlike topics_query, which joins to compute per-topic episode/show counts."""
    where, params = _topics_filter(genre, q)
    return f"SELECT COUNT(*) FROM topics t{where}", tuple(params)


PAGE_SIZE = 50


def paginate(params: dict) -> int:
    """Parse ?page=N from query params, clamped to >= 1."""
    try:
        return max(1, int(params.get("page", ["1"])[0]))
    except ValueError:
        return 1


def pagination_html(path: str, query: dict, page: int, total: int, label: str,
                     page_size: int = PAGE_SIZE) -> str:
    if total <= page_size:
        return ""
    last = (total - 1) // page_size + 1
    page = min(page, last)

    def link(p: int) -> str:
        return f"{path}?{urllib.parse.urlencode({**query, 'page': p})}"

    prev = f'<a href="{link(page - 1)}">&laquo; prev</a>' if page > 1 else '<span class="dim">&laquo; prev</span>'
    nxt = f'<a href="{link(page + 1)}">next &raquo;</a>' if page < last else '<span class="dim">next &raquo;</span>'
    return (f'<p class="dim">{prev} &nbsp; page {page} of {last} '
            f'({total} {esc(label)}) &nbsp; {nxt}</p>')


ACTIVE_WINDOW = timedelta(minutes=15)


def index_status_html(pending_episodes: int, pending_canon: int, last_extracted_at: str | None) -> str:
    """Banner on the home page showing whether extraction/canonicalization
    is currently running, stalled, or done — so a background load run is
    visible from the UI, not just inferrable from the raw counts."""
    last_dt = parse_iso(last_extracted_at) if last_extracted_at else None
    lines = []
    if pending_episodes == 0:
        if last_dt:
            lines.append(f"<p>Fully indexed — last processed {relative_time(last_dt)}.</p>")
        active = False
    else:
        active = last_dt is not None and utcnow() - last_dt < ACTIVE_WINDOW
        if active:
            lines.append(
                f"<p>Indexing in progress — {plural(pending_episodes, 'episode')} queued, "
                f"last processed {relative_time(last_dt)}.</p>"
            )
        else:
            when = f", last activity {relative_time(last_dt)}" if last_dt else ""
            lines.append(f"<p>{plural(pending_episodes, 'episode')} not yet indexed{when}.</p>")
    if pending_canon:
        lines.append(
            f"<p class=\"dim\">{plural(pending_canon, 'topic')} awaiting Wikidata canonicalization.</p>"
        )
    if not lines:
        return ""
    cls = "status active" if active else "status"
    return f'<div class="{cls}">{"".join(lines)}</div>'


def conf(value) -> str:
    return f"{value:.2f}" if value is not None else "–"


def episode_cell(row) -> str:
    title = f'<a href="/episode/{row["id"]}">{esc(row["title"])}</a>'
    url = row["audio_url"] or ""
    # Enclosure URLs come from third-party feeds; only link plain http(s) so a
    # hostile feed can't smuggle a javascript:/data: scheme into an href.
    if urllib.parse.urlsplit(url).scheme in ("http", "https"):
        return (f'{title} <a class="qid" href="{esc(url)}" rel="noreferrer" '
                f'title="play episode audio">▶</a>')
    return title


def topic_pills(topics, extracted_at) -> str:
    """Per-episode topic links for the show page, or a note when an episode
    hasn't been indexed yet (as opposed to genuinely having no subject)."""
    if not topics:
        return "" if extracted_at else '<span class="dim">not yet indexed</span>'
    return " ".join(f'<a class="pill" href="/topic/{t["topic_id"]}">{esc(t["label"])}</a>' for t in topics)


def topic_table(rows, empty: str = "Nothing here yet.") -> str:
    if not rows:
        return f'<p class="dim">{esc(empty)}</p>'
    body = "".join(
        f"<tr><td><a href='/topic/{r['id']}'>{esc(r['label'])}</a></td>"
        f"<td class='num'>{r['shows']}</td><td class='num'>{r['episodes']}</td>"
        f"<td class='dim'>{esc(r['genres'].replace(',', ', '))}</td></tr>"
        for r in rows
    )
    return ("<table><tr><th>topic</th><th>shows</th><th>episodes</th><th>genres</th></tr>"
            f"{body}</table>")


LOGIN_PAGE = """<div class="login-box">
<h1>hark</h1>
{err}
<form method="post" action="/login">
<label>User</label><input type="text" name="username" autofocus>
<label>Password</label><input type="password" name="password">
<button>Sign in</button>
</form></div>"""


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    app: App  # set by make_server
    server_version = f"hark/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet access log
        pass

    def log_error(self, fmt, *args):  # bypass log_message so errors still surface
        BaseHTTPRequestHandler.log_message(self, fmt, *args)

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
        if route.startswith("/feed/"):
            return self._serve_feed(route)
        if route.startswith("/audio/"):
            return self._serve_audio(route)

        user = app.auth.session_user(self.cookie_token())
        if user is None:
            return self.redirect("/login")

        try:
            if route == "/":
                return self.respond(200, app.view_home(user))
            if route == "/topics":
                return self.respond(200, app.view_topics(user, params))
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
                return self.respond(200, app.view_shows(user))
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
        except sqlite3.OperationalError:
            return self.db_unavailable(user)
        return self.not_found(user)

    def do_POST(self):
        app = self.app
        route = self.path.rstrip("/")
        form = self.form()  # always drain the body, even on routes that ignore it

        if route == "/login":
            user_id = app.auth.verify(form.get("username", ""), form.get("password", ""))
            if user_id is None:
                time.sleep(0.4)  # blunt brute-force throttle
                body = page("login", LOGIN_PAGE.format(err='<p class="err">No.</p>'))
                return self.respond(HTTPStatus.UNAUTHORIZED, body)
            token = app.auth.create_session(user_id)
            return self.redirect("/", {"Set-Cookie": app.cookie_attrs(token, SESSION_DAYS * 86_400)})

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
            if route.startswith("/show/") and route.endswith("/adblock"):
                try:
                    show_id = int(route.removeprefix("/show/").removesuffix("/adblock"))
                except ValueError:
                    return self.not_found(user)
                if app.toggle_ad_stripping(show_id) is None:
                    return self.not_found(user)
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
