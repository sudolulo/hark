"""App: routing + views over hark.db — split out of web.py (2026-07-13) as
that module grew past ~1900 lines mixing this with auth, HTML templates,
and HTTP routing.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from pathlib import Path

from . import claims, gpodder_server, podcast_feed
from .auth import Auth, parse_iso
from .extract import GENRES as GENRES_FILTER
from .queries import (
    PAGE_SIZE,
    contested_topics,
    paginate,
    rare_genre_episodes,
    related_shows,
    related_topics,
    topics_count,
    topics_query,
)
from .templates import (
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

# MAX_SHOWS_PER_USER lives in gpodder_server.py — shared with the
# AntennaPod-sync path so the cap is identical regardless of whether a show
# gets added via sync or the web UI's "add to my list". The admin account
# itself is exempt (see Auth.is_admin()).
MAX_SHOWS_PER_USER = gpodder_server.MAX_SHOWS_PER_USER

# Also used by web.py's Handler.cookie_token() (reads the cookie this same
# name writes) — imported from here rather than duplicated, since web.py
# already imports App from this module.
COOKIE = "hark_session"


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
        so it's never itself blocked by being at the limit).

        BEGIN IMMEDIATE takes the write lock up front, before the quota
        count below — the same hazard _toggle_show_flag's own comment
        already calls out, just spanning several statements instead of one
        atomic UPDATE. Without it, two concurrent subscribe() calls for the
        same user (double-click, two tabs — this server is threaded) can
        each read a count under the cap before either has inserted, letting
        both through and landing above MAX_SHOWS_PER_USER."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM shows WHERE id = ?", (show_id,)).fetchone() is None:
                conn.rollback()
                return None
            already_subscribed = conn.execute(
                "SELECT 1 FROM user_shows WHERE user_id = ? AND show_id = ?", (user_id, show_id)
            ).fetchone() is not None
            if not already_subscribed and not self.auth.is_admin(user_id):
                count = conn.execute(
                    "SELECT COUNT(*) FROM user_shows WHERE user_id = ?", (user_id,)
                ).fetchone()[0]
                if count >= MAX_SHOWS_PER_USER:
                    conn.rollback()
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


