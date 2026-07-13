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

import base64
import json
import secrets
import sqlite3
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, claims, gpodder_server, podcast_feed
from .auth import (  # noqa: F401 — Auth/iso/utcnow/INVITE_EXPIRES_DAYS re-exported for callers
    INVITE_EXPIRES_DAYS,
    SESSION_DAYS,
    Auth,
    iso,
    parse_iso,
    utcnow,
)
from .extract import GENRES as GENRES_FILTER
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

COOKIE = "hark_session"
MAX_FORM_BYTES = 65536
# AntennaPod batches episode-action uploads at 30 per request (its own
# UPLOAD_BULK_SIZE) — plenty of headroom over that for a JSON body.
MAX_JSON_BODY_BYTES = 1_000_000

# MAX_SHOWS_PER_USER lives in gpodder_server.py — shared with the
# AntennaPod-sync path so the cap is identical regardless of whether a show
# gets added via sync or the web UI's "add to my list". The admin account
# itself is exempt (see Auth.is_admin()).
MAX_SHOWS_PER_USER = gpodder_server.MAX_SHOWS_PER_USER


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

    def _toggle_show_flag(self, show_id: int, column: str) -> bool | None:
        """Flip a boolean column on `shows` (ad_stripping_enabled,
        topic_index_enabled). Returns the new state, or None if the show
        doesn't exist. `column` is only ever one of the two hardcoded
        literals below — never user input — so building the UPDATE with an
        f-string here doesn't open a SQL-injection surface.

        Admin-only (gated at the route, not here) — these are global,
        shared-across-every-account settings, unlike subscribe/unsubscribe
        below which are per-user. Both write into hark.db, not auth.db (see
        module docstring: data snapshots pushed from the pipeline replace
        hark.db wholesale) — a toggle or subscription set here will be lost
        on the next such re-sync unless the source-side hark.db (wherever
        the pipeline actually runs) is updated to match — same caveat as any
        other hark.db value set outside the pipeline host.
        """
        # A single atomic UPDATE (flip computed in SQL, not read-then-write in
        # Python) so two concurrent toggles can't both read the same starting
        # state and collapse into one net change instead of canceling out.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                f"UPDATE shows SET {column} = 1 - {column} WHERE id = ?", (show_id,)
            )
            if cur.rowcount == 0:
                return None
            conn.commit()
            new_state = conn.execute(
                f"SELECT {column} FROM shows WHERE id = ?", (show_id,)
            ).fetchone()[column]
            return bool(new_state)
        finally:
            conn.close()

    def toggle_ad_stripping(self, show_id: int) -> bool | None:
        return self._toggle_show_flag(show_id, "ad_stripping_enabled")

    def toggle_topic_index(self, show_id: int) -> bool | None:
        return self._toggle_show_flag(show_id, "topic_index_enabled")

    def subscribe(self, user_id: int, show_id: int) -> bool | None:
        """Add a show to one account's personal list. Returns None if the
        show doesn't exist, False if it would push a non-admin account over
        MAX_SHOWS_PER_USER, True on success. Re-subscribing to a show
        already in the list is idempotent (checked before the quota count,
        so it's never itself blocked by being at the limit)."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            if conn.execute("SELECT 1 FROM shows WHERE id = ?", (show_id,)).fetchone() is None:
                return None
            already_subscribed = conn.execute(
                "SELECT 1 FROM user_shows WHERE user_id = ? AND show_id = ?", (user_id, show_id)
            ).fetchone() is not None
            if not already_subscribed and not self.auth.is_admin(user_id):
                count = conn.execute(
                    "SELECT COUNT(*) FROM user_shows WHERE user_id = ?", (user_id,)
                ).fetchone()[0]
                if count >= MAX_SHOWS_PER_USER:
                    return False
            conn.execute(
                "INSERT OR IGNORE INTO user_shows (user_id, show_id) VALUES (?, ?)",
                (user_id, show_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def unsubscribe(self, user_id: int, show_id: int) -> None:
        """Remove a show from one account's personal list. Never touches the
        global `shows` row — same never-delete-the-show stance gpodder_server
        already has for AntennaPod-driven unsubscribes."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "DELETE FROM user_shows WHERE user_id = ? AND show_id = ?", (user_id, show_id)
            )
            conn.commit()
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
            transcribe_pending = conn.execute(
                """
                SELECT COUNT(*) FROM episodes
                WHERE transcript_path IS NULL AND audio_url IS NOT NULL
                  AND id NOT IN (SELECT episode_id FROM ad_segments WHERE source = 'chapter')
                """
            ).fetchone()[0]
            detect_pending = conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE transcript_path IS NOT NULL AND llm_detected_at IS NULL"
            ).fetchone()[0]
            cut_pending = conn.execute(
                """
                SELECT COUNT(*) FROM episodes
                WHERE cut_path IS NULL AND EXISTS
                    (SELECT 1 FROM ad_segments WHERE episode_id = episodes.id)
                """
            ).fetchone()[0]
            compare_pending = claims.count_pending_topics(conn)
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
        pipeline_status = pipeline_status_html(
            transcribe_pending, detect_pending, cut_pending, compare_pending
        )
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
            "<h1>Who covered it?</h1>" + status + pipeline_status +
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

    def view_notable(self, user) -> str:
        """Two interim "notable" signals, distinct from the home page's
        cross-show-coverage ranking (which already surfaces "most covered" —
        repeating that here would just be the same table twice). Both are
        explicitly provisional: PLAN.md's M4 (interestingness scoring,
        calibrated against real listening) is the eventual real version of
        this page; these are what's derivable without it."""
        conn = self.db()
        try:
            contested = contested_topics(conn, limit=15)
            rare_genres, rare = rare_genre_episodes(conn, limit=15)
        finally:
            conn.close()
        contested_html = "".join(
            f"<tr><td><a href='/topic/{r['topic_id']}'>{esc(r['label'])}</a></td>"
            f"<td class='num'>{r['shared_count']}</td><td class='num'>{r['unique_count']}</td>"
            f"<td class='dim'>{esc(', '.join(r['genres']))}</td></tr>"
            for r in contested
        )
        rare_html = "".join(
            f"<tr><td><a href='/episode/{r['id']}'>{esc(r['title'])}</a></td>"
            f"<td><a href='/show/{r['show_id']}'>{esc(r['show'])}</a></td>"
            f"<td><a href='/topic/{r['topic_id']}'>{esc(r['label'])}</a></td>"
            f"<td class='dim'>{esc(r['genre'])}</td></tr>"
            for r in rare
        )
        body = (
            "<h1>Notable</h1>"
            '<p class="dim">Interim signals, not M4\'s real interestingness scoring yet — '
            "just what's derivable from what hark already has.</p>"
            "<h2>Most contested</h2>"
            '<p class="dim">Topics where shows\' tellings diverge the most — highest count of '
            "claims unique to one show, among topics with a claims comparison loaded.</p>" +
            (f'<table><tr><th>topic</th><th>shared claims</th><th>unique claims</th>'
             f'<th>genres</th></tr>{contested_html}</table>' if contested else
             '<p class="dim">No claims comparisons loaded yet.</p>') +
            "<h2>Rare coverage</h2>" +
            (f'<p class="dim">Episodes covering hark\'s least-common genres — '
             f"{', '.join(rare_genres)}.</p>"
             f'<table><tr><th>episode</th><th>show</th><th>topic</th><th>genre</th></tr>'
             f'{rare_html}</table>' if rare else '<p class="dim">Nothing yet.</p>')
        )
        return page("notable", body, user["username"])

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
                       e.title, e.pubdate, e.audio_url, e.transcript_path, et.confidence
                FROM episode_topics et
                JOIN episodes e ON e.id = et.episode_id
                JOIN shows s ON s.id = e.show_id
                WHERE et.topic_id = ?
                ORDER BY show, e.pubdate
                """,
                (topic_id,),
            ).fetchall()
            related = related_topics(conn, topic_id)
            comparison = claims.get_comparison(conn, topic_id)
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
        related_html = ""
        if related:
            related_pills = " ".join(
                f'<a class="pill" href="/topic/{r["id"]}">{esc(r["label"])} '
                f'({plural(r["episodes"], "episode")})</a>'
                for r in related
            )
            related_html = f"<h2>Related topics</h2><p>{related_pills}</p>"
        shows_transcribed = {r["show_id"] for r in episodes if r["transcript_path"] is not None}
        compare_note = ""
        if comparison is not None:
            compare_note = (
                '<p class="dim">Cross-show claims comparison available — see each '
                "episode's page for shared vs. unique claims.</p>"
            )
        elif len(shows_transcribed) >= 2:
            compare_note = (
                '<p class="pending">Transcribed by 2+ shows but not compared yet.</p>'
            )
        body = (
            f"<h1>{esc(topic['label'])}{qid}</h1><p>{pills}</p>"
            f"<h2>covered by {plural(len(shows), 'show')}, {plural(len(episodes), 'episode')}</h2>"
            f"<p>{show_pills}</p>"
            f"{compare_note}"
            f"{related_html}"
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
            body += f"<h2>{plural(episode_total, 'episode title match', 'episode title matches')}</h2>{eps_table}{note}"
        return page("search", body, user["username"])

    def view_shows(self, user, params: dict) -> str:
        show_all = bool(params.get("all", ["0"])[0] == "1")
        conn = self.db()
        try:
            rows = conn.execute(
                f"""
                SELECT s.id, COALESCE(s.title, s.query) AS name, s.topic_index_enabled,
                       COUNT(e.id) AS episodes,
                       COALESCE(SUM(e.extracted_at IS NOT NULL), 0) AS extracted,
                       MAX(e.pubdate) AS latest,
                       us.user_id IS NOT NULL AS subscribed
                FROM shows s LEFT JOIN episodes e ON e.show_id = s.id
                LEFT JOIN user_shows us ON us.show_id = s.id AND us.user_id = ?
                {"" if show_all else "WHERE us.user_id IS NOT NULL"}
                GROUP BY s.id ORDER BY name
                """,
                (user["id"],),
            ).fetchall()
        finally:
            conn.close()
        unreviewed = sum(1 for r in rows if not r["topic_index_enabled"])
        note = (
            f'<p class="pending">{plural(unreviewed, "show")} not yet reviewed for the topic '
            "index — open one to enable it there.</p>"
        ) if unreviewed else ""
        toggle_link = ('<a href="/shows">« just my shows</a>' if show_all
                        else '<a href="/shows?all=1">browse every show »</a>')
        table = "".join(
            f"<tr><td><a href='/show/{r['id']}'>{esc(r['name'])}</a>"
            + ('' if r["topic_index_enabled"] else ' <span class="pill">unreviewed</span>')
            + ('' if r["subscribed"] else ' <span class="pill">not in my list</span>') +
            "</td>"
            f"<td class='num'>{r['episodes']}</td>"
            f"<td class='num{'' if r['extracted'] == r['episodes'] else ' pending'}'>{r['extracted']}</td>"
            f"<td class='dim'>{esc((r['latest'] or '')[:10])}</td></tr>"
            for r in rows
        )
        empty = ('<p class="dim">Nothing in your list yet — browse every show and subscribe, '
                 "or point AntennaPod's gpodder sync at hark and it'll fill in from there.</p>"
                 if not rows and not show_all else "")
        body = (f"<h1>shows</h1><p>{toggle_link}</p>" + note + empty +
                ("<table><tr><th>show</th><th>episodes</th>"
                 f"<th>indexed</th><th>latest</th></tr>{table}</table>" if rows else ""))
        return page("shows", body, user["username"])

    def view_show(self, user, show_id: int, params) -> str | None:
        page_num = paginate(params)
        conn = self.db()
        try:
            show = conn.execute(
                """
                SELECT id, COALESCE(title, query) AS name, feed_token,
                       ad_stripping_enabled, topic_index_enabled
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
            ad_stripping_progress = conn.execute(
                """
                SELECT COALESCE(SUM(transcript_path IS NOT NULL), 0) AS transcribed,
                       COALESCE(SUM(llm_detected_at IS NOT NULL), 0) AS detected,
                       COALESCE(SUM(cut_path IS NOT NULL), 0) AS cut
                FROM episodes WHERE show_id = ?
                """,
                (show_id,),
            ).fetchone()
            extracted_count = conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE show_id = ? AND extracted_at IS NOT NULL",
                (show_id,),
            ).fetchone()[0]
            related = related_shows(conn, show_id)
            subscribed = conn.execute(
                "SELECT 1 FROM user_shows WHERE user_id = ? AND show_id = ?", (user["id"], show_id)
            ).fetchone() is not None
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
        feed_url = podcast_feed.feed_url(show, self.base_url)
        quota_note = ""
        if params.get("err", [""])[0] == "quota" and not subscribed:
            quota_note = (
                f'<p class="pending">You\'ve reached your {MAX_SHOWS_PER_USER}-podcast limit — '
                "remove one from your list before adding another.</p>"
            )
        subscribe_section = (
            '<div class="status">'
            f'<p>{"In your list." if subscribed else "Not in your list yet."}</p>'
            f"{quota_note}"
            f'<form method="post" action="/show/{show_id}/'
            f'{"unsubscribe" if subscribed else "subscribe"}">'
            f'<button class="ghost">{"Remove from my list" if subscribed else "Add to my list"}'
            "</button></form></div>"
        )
        enabled = bool(show["ad_stripping_enabled"])
        toggle_label = "Disable ad-stripping" if enabled else "Enable ad-stripping"
        adblock_section = (
            '<div class="status">'
            f'<p>Ad-stripped feed URL — subscribe to this in AntennaPod instead of the '
            f'original: <code>{esc(feed_url)}</code></p>'
            f'<p>Ad-stripping pipeline for this show: '
            f'<strong>{"enabled" if enabled else "disabled"}</strong> — episodes only get '
            f'transcribed/scanned/cut while enabled (existing cut episodes stay cut either way).</p>'
            f'<p class="dim">{ad_stripping_progress["transcribed"]}/{total_episodes} transcribed, '
            f'{ad_stripping_progress["detected"]}/{total_episodes} ad-scanned, '
            f'{ad_stripping_progress["cut"]}/{total_episodes} cut.</p>'
            f'<form method="post" action="/show/{show_id}/adblock">'
            f'<button class="ghost">{toggle_label}</button></form>'
            "</div>"
        )
        topic_index_on = bool(show["topic_index_enabled"])
        topic_toggle_label = "Remove from topic index" if topic_index_on else "Add to topic index"
        topic_index_section = (
            '<div class="status">'
            f'<p>Topic index for this show: <strong>{"enabled" if topic_index_on else "disabled"}</strong>'
            + ("" if topic_index_on else
               ' — episodes won\'t be checked for a real-world subject until enabled. New shows '
               'start disabled until reviewed (most subscriptions aren\'t subject-per-episode '
               'genre shows, and extraction on one that isn\'t just burns effort for an empty '
               'result every time).') +
            "</p>"
            f'<p class="dim">{extracted_count}/{total_episodes} extracted.</p>'
            f'<form method="post" action="/show/{show_id}/topic-index">'
            f'<button class="ghost">{topic_toggle_label}</button></form>'
            "</div>"
        )
        related_html = ""
        if related:
            pills = " ".join(
                f'<a class="pill" href="/show/{r["id"]}">{esc(r["name"])} '
                f'({plural(r["shared"], "shared topic")})</a>'
                for r in related
            )
            related_html = f"<h2>Related shows</h2><p>{pills}</p>"
        body = (
            f"<h1>{esc(show['name'])}</h1>"
            f"<h2>{plural(total_episodes, 'episode')}, {plural(topic_count, 'topic')} covered</h2>"
            f"{subscribe_section}"
            # Global, shared-across-every-account settings — admin only (see
            # _toggle_show_flag); hidden here to match the route's own 403,
            # rather than showing a button that just fails when clicked.
            + (f"{adblock_section}{topic_index_section}" if user["is_admin"] else "") +
            f"{related_html}"
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
                SELECT t.id AS topic_id, t.label, et.confidence
                FROM episode_topics et JOIN topics t ON t.id = et.topic_id
                WHERE et.episode_id = ? ORDER BY t.label
                """,
                (episode_id,),
            ).fetchall()
            topic_sections = []
            for t in topics:
                comparison = claims.get_comparison(conn, t["topic_id"])
                shows_transcribed = conn.execute(
                    """
                    SELECT COUNT(DISTINCT e2.show_id) FROM episode_topics et2
                    JOIN episodes e2 ON e2.id = et2.episode_id
                    WHERE et2.topic_id = ? AND e2.transcript_path IS NOT NULL
                    """,
                    (t["topic_id"],),
                ).fetchone()[0]
                topic_sections.append((t, comparison, shows_transcribed))
        finally:
            conn.close()

        # True: this page has its own richer empty-state messaging below
        # (distinguishing "not yet indexed" from "no subject identified"),
        # so topic_pills' own extracted_at-aware empty message is unwanted
        # here — force it to just return "" when there are no topics.
        pills = topic_pills(topics, True)
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
                body += '<p class="dim">Transcribed by 2+ shows but not compared yet.</p>'
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

    def view_admin_users(self, user, params: dict, msg: str = "") -> str:
        """Admin-only. Web-UI equivalent of `hark user add/invite/remove` —
        exists mainly because the admin might not have shell access to the
        deployed container to run those directly (this project's own homelab
        deploy doesn't currently expose one)."""
        accounts = self.auth.list_users()
        conn = self.db()
        try:
            counts = {
                r["user_id"]: r["n"] for r in conn.execute(
                    "SELECT user_id, COUNT(*) AS n FROM user_shows GROUP BY user_id"
                )
            }
        finally:
            conn.close()
        invite_link = params.get("invite_link", [""])[0]
        link_note = (
            f'<p class="pending">Invite created — send this link: '
            f'<code>{esc(invite_link)}</code></p>'
        ) if invite_link else ""
        note = f"<p>{esc(msg)}</p>" if msg else ""

        def status_cell(a) -> str:
            if not a["invite_pending"]:
                return "" if a["has_password"] else "no password"
            # The link stays visible for as long as the invite itself does
            # (list_users() now returns the raw token) — otherwise it only
            # ever existed transiently, in the one redirect right after
            # creation, with no way to re-find it if that got lost.
            link = f"{self.base_url}/invite/{a['invite_token']}"
            return f'invite pending — <code>{esc(link)}</code>'

        rows = "".join(
            f"<tr><td>{esc(a['username'])}</td>"
            f"<td class='dim'>{'admin' if a['is_admin'] else ''}</td>"
            f"<td class='num'>{counts.get(a['id'], 0)}"
            f"{'' if a['is_admin'] else f'/{MAX_SHOWS_PER_USER}'}</td>"
            f"<td class='dim'>{status_cell(a)}</td>"
            + (
                '<td><form method="post" action="/admin/users/remove">'
                f'<input type="hidden" name="username" value="{esc(a["username"])}">'
                '<button class="ghost">Remove</button></form></td>'
                if a["username"] != user["username"] else "<td></td>"
            ) + "</tr>"
            for a in accounts
        )
        body = f"""<h1>users</h1>{link_note}{note}
<table><tr><th>account</th><th>role</th><th>podcasts</th><th>status</th><th></th></tr>
{rows}</table>
<h2>Invite someone</h2>
<form method="post" action="/admin/users/invite" class="login-box">
<label>Username</label><input type="text" name="username" required autofocus>
<label><input type="checkbox" name="is_admin" value="1"> Admin</label>
<button>Create invite link</button>
</form>"""
        return page("users", body, user["username"])


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
