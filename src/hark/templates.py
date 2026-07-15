"""HTML templates and presentation helpers — split out of web.py
(2026-07-13) as that module grew past ~1900 lines mixing these with auth,
DB queries, and HTTP routing. Dependency-free by design (stdlib `html`
only) so the CSP can stay strict (default-src 'self', no inline anything) —
single embedded stylesheet served from /static/style.css.
"""

from __future__ import annotations

import html
import urllib.parse
from datetime import timedelta

from .auth import parse_iso, utcnow
from .queries import PAGE_SIZE

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
/* .login-box defaults to vertically-centering itself as a whole page's only
   content (the actual /login and /invite/<token> screens) — .inline resets
   that for the same component reused inside a normal page (e.g. the invite
   form on /admin/users), same idea as .account-actions' override above. */
.login-box.inline { margin:1rem 0; }
.err { color:#e07a5f; }
.qid { font-size:0.78rem; color:var(--dim); }
.status { border:1px solid var(--line); border-left:3px solid var(--dim); background:var(--panel); padding:0.6rem 1rem; margin:1rem 0; font-size:0.9rem; }
.status.active { border-left-color:var(--acc); }
.status p { margin:0.2rem 0; }
.pending { color:var(--acc); }
ul.claims { margin:0.2rem 0 1rem 1.2rem; padding:0; }
ul.claims li { margin-bottom:0.3rem; }
code { background:var(--panel); border:1px solid var(--line); padding:0 0.3rem; font-size:0.9rem; }
header nav a.active { color:var(--ink); border-bottom:1px solid var(--acc); }
.pill.active { background:var(--acc); border-color:var(--acc); color:#151007; }
.crumbs { font-size:0.82rem; color:var(--dim); margin:0 0 0.6rem; }
.crumbs a { color:var(--dim); } .crumbs a:hover { color:var(--acc); }
.copy-row { display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap; }
.copy-row code { flex:1; min-width:0; overflow-wrap:anywhere; }
button.copy { padding:0.2rem 0.6rem; font-size:0.82rem; }
.tabs { display:flex; gap:0.4rem; margin:0.8rem 0 1.2rem; flex-wrap:wrap; }
.confirm-row { display:flex; gap:0.6rem; align-items:center; flex-wrap:wrap; }
.confirm-row form { display:inline; }
"""

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title} — hark</title>
<link rel="stylesheet" href="/static/style.css">
<script src="/static/app.js" defer></script>
</head><body>
{header}
<main>
{body}
</main>
</body></html>"""

HEADER = """<header>
<a class="brand" href="/">HARK</a>
<nav>{nav}</nav>
<span class="spacer"></span>
<nav><span class="dim">{user}</span> {admin_link}<a href="/account">account</a></nav>
</header>"""

# (nav key, href, label) — order matches the header's visual order. Keyed
# separately from href since a detail page (e.g. /topic/<id>) highlights an
# ancestor section (topics) rather than having its own nav entry.
NAV_ITEMS = [
    ("topics", "/topics", "topics"),
    ("shows", "/shows", "shows"),
    ("notable", "/notable", "notable"),
    ("search", "/search", "search"),
]

# Tiny same-origin script (CSP: default-src 'self' already allows this,
# no inline handlers anywhere) — the only client-side behavior in the app.
APP_JS = """
document.addEventListener("click", function (ev) {
  var btn = ev.target.closest(".copy");
  if (!btn) return;
  var target = document.getElementById(btn.dataset.copyTarget);
  if (!target || !navigator.clipboard) return;
  navigator.clipboard.writeText(target.textContent).then(function () {
    var was = btn.textContent;
    btn.textContent = "copied";
    setTimeout(function () { btn.textContent = was; }, 1500);
  });
});
"""

LOGIN_PAGE = """<div class="login-box">
<h1>hark</h1>
{err}
<form method="post" action="/login">
<label>User</label><input type="text" name="username" autofocus>
<label>Password</label><input type="password" name="password">
<button>Sign in</button>
</form></div>"""

INVITE_PAGE = """<div class="login-box">
<h1>hark</h1>
<p>You've been invited as <strong>{username}</strong>. Set a password to get started.</p>
{err}
<form method="post" action="/invite/{token}">
<label>Password</label><input type="password" name="password" minlength="8" autofocus required>
<label>Repeat</label><input type="password" name="password2" minlength="8" required>
<button>Set password</button>
</form></div>"""

INVITE_INVALID_PAGE = """<div class="login-box">
<h1>hark</h1>
<p class="err">This invite link is invalid or has expired — ask whoever invited you for a new one.</p>
</div>"""

ACTIVE_WINDOW = timedelta(minutes=15)


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def plural(n: int, word: str, plural_word: str | None = None) -> str:
    """`plural_word` overrides the naive `word + "s"` for irregular nouns
    (e.g. plural(n, "match", "matches"))."""
    return f"{n} {word}" if n == 1 else f"{n} {plural_word or word + 's'}"


def page(title: str, body: str, user: str | None = None, is_admin: bool = False,
         section: str | None = None) -> str:
    # No route for this link was navigable before — /admin/users only ever
    # showed up if you already knew to type it, despite the header otherwise
    # trying to be the single place every page is reachable from. Labeled
    # "users" (matching that page's own <h1>), not "admin" — the default
    # bootstrap account is itself named "admin", so "admin admin account"
    # would otherwise read as a stutter in the common case.
    admin_link = '<a href="/admin/users">users</a> ' if is_admin else ""
    nav = " ".join(
        f'<a href="{href}" class="active">{label}</a>' if key == section
        else f'<a href="{href}">{label}</a>'
        for key, href, label in NAV_ITEMS
    )
    header = HEADER.format(user=esc(user), admin_link=admin_link, nav=nav) if user else ""
    return PAGE.format(title=esc(title), header=header, body=body)


def breadcrumb(*crumbs: tuple[str, str | None]) -> str:
    """`crumbs` is (label, href) pairs, in order, always starting from
    ("home", "/") — the last crumb should pass href=None (the current page,
    unlinked). Renders nothing for a single-crumb (top-level) page, since
    the header nav's own active state already covers that case."""
    if len(crumbs) < 2:
        return ""
    parts = [f'<a href="{href}">{esc(label)}</a>' if href else esc(label) for label, href in crumbs]
    return f'<p class="crumbs">{" / ".join(parts)}</p>'


def copy_button(target_id: str) -> str:
    return f'<button type="button" class="copy ghost" data-copy-target="{esc(target_id)}">copy</button>'


def relative_time(dt) -> str:
    secs = (utcnow() - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


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
            assert last_dt is not None  # active can only be True per the line above
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


def pipeline_status_html(transcribe_pending: int, detect_pending: int,
                          cut_pending: int, compare_pending: int) -> str:
    """Home page banner for the ad-stripping + claims-comparison pipeline —
    same idea as index_status_html for extraction. Before this, checking
    what the deployed transcribe/compare loop actually has left to do meant
    querying hark.db directly instead of just looking at the dashboard."""
    if not (transcribe_pending or detect_pending or cut_pending or compare_pending):
        return ""
    lines = []
    if transcribe_pending:
        lines.append(f"<p>{plural(transcribe_pending, 'episode')} awaiting transcription.</p>")
    if detect_pending:
        lines.append(f"<p>{plural(detect_pending, 'episode')} awaiting ad-span detection.</p>")
    if cut_pending:
        lines.append(f"<p>{plural(cut_pending, 'episode')} awaiting ad cutting.</p>")
    if compare_pending:
        lines.append(
            f'<p class="pending">{plural(compare_pending, "topic")} ready for cross-show '
            f'claims comparison.</p>'
        )
    return f'<div class="status">{"".join(lines)}</div>'


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


def claims_html(comparison, shows_transcribed: int, *, compact: bool = False,
                 topic_id: int | None = None, viewer_show: str | None = None,
                 transcript_path: str | None = None) -> str:
    """Renders a topic's claims comparison. The underlying data
    (topic_comparisons) is topic-scoped — one comparison per topic, the same
    regardless of which episode you arrived from — so `/topic/<id>` renders
    it in full (compact=False) and is the canonical place to read it; an
    episode page (compact=True) gets a trimmed view scoped to that episode's
    own show, plus a pointer to the full comparison, instead of repeating
    every other show's unique-claims text on every single episode of a
    well-covered topic.

    `transcript_path` is only meaningful in compact mode (this *episode's*
    own transcript) — full mode has no single episode to ask about."""
    if comparison is None:
        if compact and transcript_path is None:
            return '<p class="dim">This episode hasn’t been transcribed yet.</p>'
        if shows_transcribed >= 2:
            return '<p class="dim">Transcribed by 2+ shows but not compared yet.</p>'
        return (
            '<p class="dim">Only this show has covered this topic so far.</p>' if compact
            else '<p class="dim">Only one show has covered this topic so far.</p>'
        )

    when = ""
    if comparison.generated_at:
        when = f' <span class="dim">— compared {relative_time(parse_iso(comparison.generated_at))}</span>'
    shared_html = "".join(f"<li>{esc(c)}</li>" for c in comparison.shared)
    html = (
        f"<p>Claims shared across shows:{when}</p><ul class='claims'>{shared_html}</ul>"
        if comparison.shared else f'<p class="dim">No claims judged shared across shows.{when}</p>'
    )

    if compact:
        own = comparison.unique_by_show.get(viewer_show or "", [])
        if own:
            items = "".join(f"<li>{esc(c)}</li>" for c in own)
            html += f"<p>Unique to this telling:</p><ul class='claims'>{items}</ul>"
        else:
            html += '<p class="dim">Nothing unique to this telling.</p>'
        others = [s for s, c in comparison.unique_by_show.items() if s != viewer_show and c]
        if others and topic_id is not None:
            html += (
                f'<p class="dim">{plural(len(others), "other show")} covered this too — '
                f'<a href="/topic/{topic_id}#comparison">see the full comparison</a>.</p>'
            )
        return html

    any_unique = False
    for show, own_claims in comparison.unique_by_show.items():
        if not own_claims:
            continue
        any_unique = True
        items = "".join(f"<li>{esc(c)}</li>" for c in own_claims)
        html += f"<p>Unique to {esc(show)}:</p><ul class='claims'>{items}</ul>"
    if not any_unique:
        html += '<p class="dim">No claims judged unique to a single show.</p>'
    return html
