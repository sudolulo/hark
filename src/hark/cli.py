"""hark command line: resolve, ingest, sync-subscriptions, sync-history,
import-opml, discover, extract, chapters, transcribe, detect-ads, cut, fsck,
compare, load-comparisons, stats, topics, who, web.

chapters/transcribe/detect-ads/cut call straight into the `adscrub` package
(a separate product, depended on as a library — see pyproject.toml) rather
than through any hark-side reimplementation: hark's episodes/ad_segments
schema was deliberately shaped to match adscrub's own, so adscrub's
schema-coupled functions (pending_episodes, scan_episode, transcribe_episode,
...) work unchanged against hark's `conn`. detect-ads/cut are a partial
exception: they call adscrub's per-episode detect_episode/cut_episode
directly in a hand-rolled loop here (not the bulk detect_pending/cut_pending
orchestrators) so hark's own per-show ad_stripping_enabled filter can apply —
see _enabled_show_ids()/_filter_enabled() and cmd_detect_ads/cmd_cut below.
Otherwise the CLI wiring here is hark's own code.
"""

from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

import httpx

from adscrub import chapters as ad_chapters
from adscrub import cut as ad_cut
from adscrub import detect as ad_detect
from adscrub import transcribe as ad_transcribe

from . import __version__, claims, db, discover, extract, ingest, nextcloud, opml, pipeline, resolve, wikidata

DEFAULT_DB = os.environ.get("HARK_DB", "hark.db")
DEFAULT_FEEDS = "feeds.txt"
DEFAULT_MODEL = os.environ.get("HARK_MODEL", extract.DEFAULT_MODEL)
USER_AGENT = f"hark/{__version__} (homelab podcast indexer)"


def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=30.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )


def make_nextcloud_client(args: argparse.Namespace) -> httpx.Client:
    """Separate from make_client(): a self-hosted Nextcloud instance on the
    LAN commonly runs behind a self-signed cert, and --nextcloud-insecure is
    an explicit, per-command opt-out of verification for that one case —
    make_client() itself must stay fully verifying for every other caller
    (Anthropic, iTunes, HF Hub, arbitrary podcast feed hosts)."""
    return httpx.Client(
        timeout=30.0, follow_redirects=True, headers={"User-Agent": USER_AGENT},
        verify=not args.nextcloud_insecure,
    )


def _enabled_show_ids(conn) -> set[int]:
    return {r["id"] for r in conn.execute("SELECT id FROM shows WHERE ad_stripping_enabled = 1")}


def _filter_enabled(episodes, enabled_ids: set[int], limit: int | None = None) -> list:
    """Filter a pending-episode list down to shows with ad_stripping_enabled,
    then apply `limit` — done here rather than at the SQL level inside
    adscrub's own pending_episodes() so hark's per-show toggle doesn't need
    any adscrub code change beyond exposing per-episode functions. Callers
    compute `enabled_ids` once per command (via _enabled_show_ids) rather
    than re-querying it on every one of a command's 2-3 call sites."""
    filtered = [e for e in episodes if e["show_id"] in enabled_ids]
    return filtered[:limit] if limit else filtered


def make_reporter() -> tuple[Callable[[pipeline.ExtractResult], None], dict[str, int]]:
    """Shared per-episode progress printer for `extract` and `load`."""
    counts = {"ok": 0, "failed": 0, "skipped": 0}

    def report(r: pipeline.ExtractResult) -> None:
        if r.error:
            counts["failed"] += 1
            print(f"  FAIL  {r.show} — {r.title}: {r.error}")
        elif r.skipped:
            counts["skipped"] += 1
            print(f"  skip  {r.show} — {r.title}: already extracted")
        else:
            counts["ok"] += 1
            labels = "; ".join(r.labels) if r.labels else "(no subject)"
            print(f"  ok    {r.show} — {r.title} -> {labels}")

    return report, counts


def cmd_resolve(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    names = resolve.read_feeds_file(args.feeds)
    if not names:
        print(f"no show names found in {args.feeds}", file=sys.stderr)
        return 1
    with make_client() as client:
        results = resolve.resolve_all(conn, client, names)
    misses = 0
    for name, show in results:
        if show is None:
            misses += 1
            print(f"  MISS  {name}")
        else:
            print(f"  ok    {name} -> {show.feed_url}")
    print(f"resolved {len(results) - misses}/{len(results)} shows")
    return 1 if misses else 0


def cmd_ingest(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    with make_client() as client:
        results = ingest.ingest_all(conn, client)
    if not results:
        print("no resolved shows to ingest — run `hark resolve` first", file=sys.stderr)
        return 1
    errors = 0
    for r in results:
        if r.error:
            errors += 1
            print(f"  FAIL  {r.query}: {r.error}")
        else:
            print(f"  ok    {r.query}: +{r.inserted} new, {r.updated} updated ({r.total} in feed)")
    return 1 if errors else 0


def _nextcloud_configured(args: argparse.Namespace) -> bool:
    if args.nextcloud_url and args.nextcloud_user and args.nextcloud_password:
        return True
    print("hint: set $HARK_NEXTCLOUD_URL, $HARK_NEXTCLOUD_USER, $HARK_NEXTCLOUD_PASSWORD "
          "(an app password, not the account password) — same account AntennaPod "
          "itself syncs to via the GPodder Sync app", file=sys.stderr)
    return False


def cmd_sync_subscriptions(args: argparse.Namespace) -> int:
    """M3: register any show in the Nextcloud gpodder subscription list that
    hark doesn't already know, so new AntennaPod subscriptions reach the
    ad-stripping pipeline without hand-editing feeds.txt. Never removes a
    show on gpodder-side unsubscribe — hark's topic index stays a durable
    "who covered X" record independent of what you're still subscribed to."""
    if not _nextcloud_configured(args):
        return 1
    conn = db.connect(args.db)
    auth = (args.nextcloud_user, args.nextcloud_password)
    with make_nextcloud_client(args) as client:
        try:
            feed_urls = nextcloud.current_subscriptions(client, args.nextcloud_url, auth)
        except httpx.HTTPError as exc:
            print(f"nextcloud: {exc}", file=sys.stderr)
            return 1
    added = 0
    for feed_url in feed_urls:
        if resolve.add_show_by_feed_url(conn, feed_url):
            added += 1
            print(f"  ok    {feed_url}")
    print(f"synced {len(feed_urls)} subscription(s), {added} new "
          f"(run `hark ingest` to fetch episodes and titles)")
    return 0


def cmd_sync_history(args: argparse.Namespace) -> int:
    """M3: pull new AntennaPod play-history events into listen_actions —
    consumed by M4's scoring calibration, nothing reads it yet. Incremental
    via a stored cursor (sync_state), since this list only grows."""
    if not _nextcloud_configured(args):
        return 1
    conn = db.connect(args.db)
    cursor_row = conn.execute(
        "SELECT value FROM sync_state WHERE key = 'gpodder_episode_action_since'"
    ).fetchone()
    since = int(cursor_row["value"]) if cursor_row else 0
    auth = (args.nextcloud_user, args.nextcloud_password)
    with make_nextcloud_client(args) as client:
        try:
            actions, new_since = nextcloud.fetch_episode_actions(
                client, args.nextcloud_url, auth, since=since
            )
        except httpx.HTTPError as exc:
            print(f"nextcloud: {exc}", file=sys.stderr)
            return 1
    inserted = 0
    for a in actions:
        podcast_url, episode_url = a.get("podcast"), a.get("episode")
        if not podcast_url or not episode_url:
            continue  # malformed entry — nothing to key it on
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO listen_actions
                (podcast_url, episode_url, episode_guid, action, position, total, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (podcast_url, episode_url, a.get("guid"), a.get("action"),
             a.get("position"), a.get("total"), a.get("timestamp")),
        )
        inserted += cur.rowcount
    conn.execute(
        """
        INSERT INTO sync_state (key, value) VALUES ('gpodder_episode_action_since', ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value
        """,
        (str(new_since),),
    )
    conn.commit()
    print(f"synced {len(actions)} listen action(s) since last run, {inserted} new")
    return 0


def cmd_import_opml(args: argparse.Namespace) -> int:
    """Fallback path onto the same show-registration hark.resolve gives
    gpodder sync — for a one-off OPML export instead of (or alongside) the
    live Nextcloud account."""
    conn = db.connect(args.db)
    try:
        entries = opml.read_opml_file(args.file)
    except (OSError, ET.ParseError) as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 1
    if not entries:
        print(f"no <outline xmlUrl=...> feeds found in {args.file}", file=sys.stderr)
        return 1
    added = 0
    for entry in entries:
        if resolve.add_show_by_feed_url(conn, entry.feed_url, title=entry.title):
            added += 1
            print(f"  ok    {entry.title or entry.feed_url}")
    print(f"imported {len(entries)} feed(s) from {args.file}, {added} new "
          f"(run `hark ingest` to fetch episodes)")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """M2: cheap-signal candidate-show search — report only by default.
    `--add` registers candidates the same way sync-subscriptions/import-opml
    do (bare row, title/description filled in by the next `hark ingest`);
    without it this never touches `shows`, since the whole point is owner
    review before spending any real pipeline budget on a new show."""
    conn = db.connect(args.db)
    terms = list(discover.SEED_TERMS[args.genre]) if args.genre else None
    with make_client() as client:
        candidates = discover.search_candidates(client, terms, limit_per_term=args.limit_per_term)
    candidates = discover.filter_known(conn, candidates)[:args.limit]
    if not candidates:
        print("no new candidates found", file=sys.stderr)
        return 1
    added = 0
    for c in candidates:
        marker = "  ok    " if args.add and resolve.add_show_by_feed_url(conn, c.feed_url, title=c.title) else "        "
        if marker.strip():
            added += 1
        eps = f"{c.episode_count} eps" if c.episode_count is not None else "? eps"
        print(f"{marker}{c.title} — {c.genre or '?'}, {eps}, by {c.author or '?'} "
              f"(matched {c.matched_term!r})\n          {c.feed_url}")
    if args.add:
        print(f"found {len(candidates)} candidate(s), added {added} "
              f"(run `hark ingest` to fetch episodes)")
    else:
        print(f"found {len(candidates)} candidate(s) — re-run with --add to register any of them")
    return 0


def cmd_chapters(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    enabled = _enabled_show_ids(conn)
    episodes = _filter_enabled(ad_chapters.pending_episodes(conn), enabled)
    if not episodes:
        print("no episodes with an unscanned chapters_url (from an ad-stripping-enabled show)",
              file=sys.stderr)
        return 1
    found = 0
    with make_client() as client:
        for ep in episodes:
            try:
                n = ad_chapters.scan_episode(conn, client, ep)
            except httpx.HTTPError as exc:
                print(f"  FAIL  {ep['title'] or ''}: {exc}")
                continue
            found += n
            print(f"  ok    {ep['title'] or ''}: {n} ad span(s) from chapters")
    print(f"found {found} chapter-sourced ad span(s) across {len(episodes)} episode(s)")
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    enabled = _enabled_show_ids(conn)
    source = claims.episodes_needing_transcription(conn) if args.cross_show_only \
        else ad_transcribe.pending_episodes(conn)
    pending = _filter_enabled(source, enabled, args.limit)
    if args.dry_run:
        total_pending = len(_filter_enabled(source, enabled))
        print(f"pending episodes: {total_pending}"
              + (f" (would process {len(pending)} this run)" if args.limit else ""))
        return 0
    if not pending:
        print("no episodes pending transcription (from an ad-stripping-enabled show)",
              file=sys.stderr)
        return 1

    # Consecutive-failure abort mirrors cmd_detect_ads: a transient outage
    # (rate limit, network) otherwise burns through the entire pending list
    # every run, guaranteed to fail on every remaining episode.
    ok = errors = 0
    consecutive_errors = 0
    max_consecutive_errors = 5
    with make_client() as client:
        for ep in pending:
            try:
                path = ad_transcribe.transcribe_episode(conn, ep, client, model_size=args.model)
            except (httpx.HTTPError, OSError) as exc:
                errors += 1
                consecutive_errors += 1
                print(f"  FAIL  {ep['title'] or ''}: {exc}")
                if consecutive_errors >= max_consecutive_errors:
                    print(f"  aborting after {consecutive_errors} consecutive failures", file=sys.stderr)
                    break
                continue
            ok += 1
            consecutive_errors = 0
            print(f"  ok    {ep['title'] or ''} -> {path}")
    remaining_source = claims.episodes_needing_transcription(conn) if args.cross_show_only \
        else ad_transcribe.pending_episodes(conn)
    remaining = len(_filter_enabled(remaining_source, enabled))
    print(f"transcribed {ok} episode(s) ({errors} failed, {remaining} still pending)")
    return 1 if errors else 0


def cmd_detect_ads(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    enabled = _enabled_show_ids(conn)
    pending = _filter_enabled(ad_detect.pending_episodes(conn), enabled, args.limit)
    if args.dry_run:
        total_pending = len(_filter_enabled(ad_detect.pending_episodes(conn), enabled))
        print(f"pending episodes: {total_pending}"
              + (f" (would process {len(pending)} this run)" if args.limit else ""))
        return 0
    if not pending:
        print("no episodes pending ad-span detection (from an ad-stripping-enabled show)",
              file=sys.stderr)
        return 1

    import anthropic  # deferred: other commands must work without a key

    try:
        client = anthropic.Anthropic()
    except anthropic.AnthropicError as exc:
        print(f"anthropic client: {exc}", file=sys.stderr)
        print("hint: export ANTHROPIC_API_KEY first (it lives in rbw, not in a file)",
              file=sys.stderr)
        return 1

    detector = ad_detect.ClaudeAdDetector(client, model=args.model)

    # Uses detect_episode directly (not the bulk detect_pending) so the
    # per-show enabled filter above actually takes effect — detect_pending
    # does its own pending_episodes() query internally with no way to
    # restrict it to a specific episode set. The consecutive-failure abort
    # below is copied from detect_pending's own logic (adscrub/detect.py) to
    # preserve that behavior, since bypassing detect_pending drops it otherwise.
    ok = errors = 0
    consecutive_errors = 0
    max_consecutive_errors = 5
    for ep in pending:
        try:
            found = ad_detect.detect_episode(conn, ep, detector)
        except Exception as exc:  # noqa: BLE001 — per-episode isolation, matches detect_pending
            conn.rollback()
            errors += 1
            consecutive_errors += 1
            print(f"  FAIL  {ep['title'] or ''}: {exc}")
            if consecutive_errors >= max_consecutive_errors:
                print(f"  aborting after {consecutive_errors} consecutive failures", file=sys.stderr)
                break
            continue
        ok += 1
        consecutive_errors = 0
        print(f"  ok    {ep['title'] or ''}: {found} ad span(s) from transcript")
    # ok + errors, not len(pending) - errors: an early abort leaves part of
    # `pending` never attempted at all, which the old subtraction would have
    # miscounted as "succeeded".
    remaining = len(_filter_enabled(ad_detect.pending_episodes(conn), enabled))
    print(f"detected across {ok} episode(s) ({errors} failed, {remaining} still pending)")
    return 1 if errors else 0


def cmd_cut(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    enabled = _enabled_show_ids(conn)
    pending = _filter_enabled(ad_cut.pending_episodes(conn), enabled, args.limit)
    if args.dry_run:
        total_pending = len(_filter_enabled(ad_cut.pending_episodes(conn), enabled))
        print(f"pending episodes: {total_pending}"
              + (f" (would process {len(pending)} this run)" if args.limit else ""))
        return 0
    if not pending:
        print("no episodes pending cutting (from an ad-stripping-enabled show)", file=sys.stderr)
        return 1

    # Uses cut_episode directly (not the bulk cut_pending) — same reason as
    # cmd_detect_ads above. cut_pending never had a consecutive-failure abort
    # to begin with, so no equivalent is needed here.
    errors = 0
    with make_client() as client:
        for ep in pending:
            try:
                _path, ad_seconds = ad_cut.cut_episode(conn, ep, client)
            except Exception as exc:  # noqa: BLE001 — per-episode isolation, matches cut_pending
                errors += 1
                print(f"  FAIL  {ep['title'] or ''}: {exc}")
                continue
            print(f"  ok    {ep['title'] or ''}: removed {ad_seconds:.1f}s of ads")
    remaining = len(_filter_enabled(ad_cut.pending_episodes(conn), enabled))
    print(f"cut {len(pending) - errors} episode(s) ({errors} failed, {remaining} still pending)")
    return 1 if errors else 0


def cmd_fsck(args: argparse.Namespace) -> int:
    """Find episodes.transcript_path pointers whose file no longer exists —
    e.g. after a data-directory restore/migration that carried over
    database rows without the transcript files they reference — and clear
    them so the pipeline re-queues those episodes for real transcription
    instead of treating already-lost data as done."""
    conn = db.connect(args.db)
    rows = conn.execute(
        "SELECT id, transcript_path FROM episodes WHERE transcript_path IS NOT NULL"
    ).fetchall()
    dangling = [r for r in rows if not Path(r["transcript_path"]).is_file()]
    print(f"{len(dangling)} of {len(rows)} transcript_path pointer(s) reference a missing file")
    if not dangling:
        return 0
    if not args.fix:
        print("re-run with --fix to clear them", file=sys.stderr)
        return 1
    for r in dangling:
        conn.execute("UPDATE episodes SET transcript_path = NULL WHERE id = ?", (r["id"],))
    conn.commit()
    print(f"cleared {len(dangling)} dangling transcript_path pointer(s)")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    pending = claims.pending_topics(conn, args.limit)
    if args.dry_run:
        total_pending = len(claims.pending_topics(conn))
        print(f"pending topics: {total_pending}"
              + (f" (would process {len(pending)} this run)" if args.limit else ""))
        return 0
    if not pending:
        print("no topics pending comparison (need 2+ shows' transcripts on the same topic)",
              file=sys.stderr)
        return 1

    import anthropic  # deferred: other commands must work without a key

    try:
        client = anthropic.Anthropic()
    except anthropic.AnthropicError as exc:
        print(f"anthropic client: {exc}", file=sys.stderr)
        print("hint: export ANTHROPIC_API_KEY first (it lives in rbw, not in a file)",
              file=sys.stderr)
        return 1

    comparator = claims.ClaudeComparator(client, model=args.model)

    def report(r: claims.CompareResult) -> None:
        if r.error:
            print(f"  FAIL  {r.label}: {r.error}")
        else:
            print(f"  ok    {r.label}: {r.shared_count} shared claim(s)")

    results = claims.compare_pending(conn, comparator, limit=args.limit, on_result=report)
    errors = sum(1 for r in results if r.error)
    remaining = len(claims.pending_topics(conn))
    print(f"compared {len(results) - errors} topic(s) ({errors} failed, {remaining} still pending)")
    return 1 if errors else 0


def cmd_load_comparisons(args: argparse.Namespace) -> int:
    import json

    conn = db.connect(args.db)
    try:
        with open(args.file, encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 1
    errors = 0

    def report(r: claims.CompareResult) -> None:
        nonlocal errors
        if r.error:
            errors += 1
            print(f"  FAIL  {r.label or r.topic_id}: {r.error}")
        else:
            print(f"  ok    {r.label}: {r.shared_count} shared claim(s)")

    results = claims.load_comparisons(conn, records, model=args.source, on_result=report)
    print(f"loaded {len(results) - errors} comparison(s) ({errors} failed)")
    return 1 if errors else 0


def cmd_extract(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    pending = pipeline.pending_episodes(conn, args.limit)
    total_pending = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE extracted_at IS NULL"
    ).fetchone()[0]
    if args.dry_run or not pending:
        print(f"pending episodes: {total_pending}"
              + (f" (would process {len(pending)} this run)" if args.limit else ""))
        return 0

    import anthropic  # deferred: other commands must work without a key

    try:
        client = anthropic.Anthropic()
    except anthropic.AnthropicError as exc:
        print(f"anthropic client: {exc}", file=sys.stderr)
        print("hint: export ANTHROPIC_API_KEY first (it lives in rbw, not in a file)",
              file=sys.stderr)
        return 1

    extractor = extract.ClaudeExtractor(client, model=args.model)
    report, counts = make_reporter()

    with make_client() as http_client:
        canon = wikidata.Canonicalizer(http_client)
        pipeline.extract_pending(
            conn, extractor, canon.canonicalize, source=args.model,
            limit=args.limit, on_result=report,
        )
    remaining = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE extracted_at IS NULL"
    ).fetchone()[0]
    print(f"extracted {counts['ok']} episodes ({counts['failed']} failed, {remaining} still pending)")
    return 1 if counts["failed"] else 0


def cmd_load(args: argparse.Namespace) -> int:
    import json

    conn = db.connect(args.db)
    try:
        with open(args.file, encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 1
    ok = failed = skipped = 0

    def report(r: pipeline.ExtractResult) -> None:
        nonlocal ok, failed, skipped
        if r.error:
            failed += 1
            print(f"  FAIL  {r.show} — {r.title}: {r.error}")
        elif r.skipped:
            skipped += 1
            print(f"  skip  {r.show} — {r.title}: already extracted")
        else:
            ok += 1
            labels = "; ".join(r.labels) if r.labels else "(no subject)"
            print(f"  ok    {r.show} — {r.title} -> {labels}")

    with make_client() as http_client:
        canon = wikidata.Canonicalizer(http_client)
        pipeline.load_extractions(
            conn, records, canon.canonicalize, source=args.source, on_result=report
        )
    print(f"loaded {ok} episodes ({skipped} already loaded, {failed} failed)")
    return 1 if failed else 0


def cmd_canon(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    with make_client() as http_client:
        canon = wikidata.Canonicalizer(http_client)
        results = pipeline.recanonicalize(conn, canon.canonicalize)
    for r in results:
        action = "merged into" if r.merged else "->"
        print(f"  {r.old_label} {action} {r.new_label} [{r.qid}]")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM topics WHERE wikidata_id IS NULL"
    ).fetchone()[0]
    print(f"canonicalized {len(results)} topics ({remaining} still unmatched)")
    return 0


def cmd_topics(args: argparse.Namespace) -> int:
    from . import web  # deferred: other commands work without importing the web module

    conn = db.connect(args.db)
    rows = conn.execute(*web.topics_query(limit=args.limit)).fetchall()
    if not rows:
        print("no topics yet — run `hark extract` first", file=sys.stderr)
        return 1
    for row in rows:
        qid = row["wikidata_id"] or "-"
        print(f"  {row['label']:<48} {row['shows']} shows / {row['episodes']:>3} eps"
              f"   {qid:<12} {row['genres']}")
    return 0


def cmd_who(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    query = args.topic
    rows = conn.execute(
        """
        SELECT t.id, t.label, t.wikidata_id
        FROM topics t
        WHERE t.label LIKE ? COLLATE NOCASE OR t.wikidata_id = ?
        ORDER BY t.label
        """,
        (f"%{query}%", query),
    ).fetchall()
    if not rows:
        print(f"no topic matching {query!r}", file=sys.stderr)
        return 1
    for topic in rows:
        qid = f" [{topic['wikidata_id']}]" if topic["wikidata_id"] else ""
        print(f"{topic['label']}{qid}")
        episodes = conn.execute(
            """
            SELECT COALESCE(s.title, s.query) AS show, e.title, e.pubdate, et.confidence
            FROM episode_topics et
            JOIN episodes e ON e.id = et.episode_id
            JOIN shows s ON s.id = e.show_id
            WHERE et.topic_id = ?
            ORDER BY e.pubdate
            """,
            (topic["id"],),
        ).fetchall()
        for ep in episodes:
            date = (ep["pubdate"] or "-")[:10]
            conf = f"{ep['confidence']:.2f}" if ep["confidence"] is not None else "-"
            print(f"  {date}  {ep['show']:<30} {ep['title']}  ({conf})")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from . import web

    web.serve(
        db_path=args.db,
        auth_path=args.auth_db,
        bind=args.bind,
        admin_token=os.environ.get("HARK_ADMIN_TOKEN"),
        cookie_secure=os.environ.get("HARK_COOKIE_SECURE", "0") == "1",
        base_url=args.base_url,
    )
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    shows = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(feed_url IS NOT NULL), 0) AS resolved FROM shows"
    ).fetchone()
    episodes = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    topics = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    links = conn.execute("SELECT COUNT(*) FROM episode_topics").fetchone()[0]
    segments = conn.execute("SELECT COUNT(*) FROM ad_segments").fetchone()[0]
    cut_count = conn.execute("SELECT COUNT(*) FROM episodes WHERE cut_path IS NOT NULL").fetchone()[0]
    print(f"shows:    {shows['n']} ({shows['resolved']} resolved)")
    print(f"episodes: {episodes}")
    print(f"topics:   {topics} ({links} episode links)")
    print(f"ad_segments: {segments} ({cut_count} episodes cut)")
    rows = conn.execute(
        """
        SELECT COALESCE(s.title, s.query) AS name, COUNT(e.id) AS n,
               MAX(e.pubdate) AS latest
        FROM shows s LEFT JOIN episodes e ON e.show_id = s.id
        GROUP BY s.id ORDER BY name
        """
    ).fetchall()
    if rows:
        print()
        for row in rows:
            latest = (row["latest"] or "-")[:10]
            print(f"  {row['name']:<42} {row['n']:>5} episodes   latest {latest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hark", description="Cross-podcast topic index and discovery service."
    )
    parser.add_argument("--version", action="version", version=f"hark {__version__}")
    parser.add_argument(
        "--db", default=DEFAULT_DB,
        help=f"SQLite database path (default: $HARK_DB or {DEFAULT_DB})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("resolve", help="resolve show names in feeds.txt to feed URLs")
    p.add_argument("--feeds", default=DEFAULT_FEEDS, help="show list file (default: feeds.txt)")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("ingest", help="fetch resolved feeds and upsert episodes")
    p.set_defaults(func=cmd_ingest)

    def _add_nextcloud_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--nextcloud-url", default=os.environ.get("HARK_NEXTCLOUD_URL"),
                       help="Nextcloud base URL, e.g. https://host:9001 (default: $HARK_NEXTCLOUD_URL)")
        p.add_argument("--nextcloud-user", default=os.environ.get("HARK_NEXTCLOUD_USER"),
                       help="default: $HARK_NEXTCLOUD_USER")
        p.add_argument("--nextcloud-password", default=os.environ.get("HARK_NEXTCLOUD_PASSWORD"),
                       help="an app password, not the account password — "
                            "default: $HARK_NEXTCLOUD_PASSWORD")
        p.add_argument("--nextcloud-insecure", action="store_true",
                       default=bool(os.environ.get("HARK_NEXTCLOUD_INSECURE")),
                       help="skip TLS verification — for a self-hosted instance on a "
                            "self-signed cert (default: $HARK_NEXTCLOUD_INSECURE)")

    p = sub.add_parser(
        "sync-subscriptions",
        help="M3: register new shows from the Nextcloud gpodder subscription list",
    )
    _add_nextcloud_args(p)
    p.set_defaults(func=cmd_sync_subscriptions)

    p = sub.add_parser(
        "sync-history",
        help="M3: pull new AntennaPod listen-history events (for future M4 scoring)",
    )
    _add_nextcloud_args(p)
    p.set_defaults(func=cmd_sync_history)

    p = sub.add_parser(
        "import-opml", help="register shows by feed URL from an OPML export"
    )
    p.add_argument("file", help="OPML file to import")
    p.set_defaults(func=cmd_import_opml)

    p = sub.add_parser(
        "discover",
        help="M2: cheap-signal iTunes Search candidate shows, not yet tracked (report-only unless --add)",
    )
    p.add_argument("--genre", choices=sorted(discover.SEED_TERMS), help="restrict to one genre's seed terms")
    p.add_argument("--limit", type=int, default=20, help="max candidates to report (default: 20)")
    p.add_argument("--limit-per-term", type=int, default=10,
                   help="iTunes Search results per seed term (default: 10)")
    p.add_argument("--add", action="store_true",
                   help="register candidates as shows (default: report only)")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("chapters", help="scan episodes' existing chapter markers for ad spans")
    p.set_defaults(func=cmd_chapters)

    p = sub.add_parser(
        "transcribe", help="transcribe episodes with no chapter-sourced ad spans"
    )
    p.add_argument("--limit", type=int, help="max episodes to process this run")
    p.add_argument("--model", default=os.environ.get("HARK_WHISPER_MODEL", ad_transcribe.DEFAULT_MODEL),
                   help=f"faster-whisper model size (default: $HARK_WHISPER_MODEL or "
                        f"{ad_transcribe.DEFAULT_MODEL})")
    p.add_argument("--dry-run", action="store_true",
                   help="only report how many episodes are pending")
    p.add_argument("--cross-show-only", action="store_true",
                   help="only episodes covering a topic 2+ shows have also covered — "
                        "the priority subset claims comparison actually needs, instead "
                        "of every episode with audio (adscrub's default scope)")
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("detect-ads", help="classify ad spans from transcripts with a Claude model")
    p.add_argument("--limit", type=int, help="max episodes to process this run")
    p.add_argument("--model", default=os.environ.get("HARK_AD_MODEL", ad_detect.DEFAULT_MODEL),
                   help=f"Claude model id (default: $HARK_AD_MODEL or {ad_detect.DEFAULT_MODEL})")
    p.add_argument("--dry-run", action="store_true",
                   help="only report how many episodes are pending")
    p.set_defaults(func=cmd_detect_ads)

    p = sub.add_parser("cut", help="cut ad spans out of episode audio with ffmpeg")
    p.add_argument("--limit", type=int, help="max episodes to process this run")
    p.add_argument("--dry-run", action="store_true",
                   help="only report how many episodes are pending")
    p.set_defaults(func=cmd_cut)

    p = sub.add_parser(
        "fsck", help="find and clear transcript_path pointers whose file is missing"
    )
    p.add_argument("--fix", action="store_true", help="clear dangling pointers (default: report only)")
    p.set_defaults(func=cmd_fsck)

    p = sub.add_parser("extract", help="extract episode topics with a Claude model")
    p.add_argument("--limit", type=int, help="max episodes to process this run")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Claude model id (default: $HARK_MODEL or {extract.DEFAULT_MODEL})")
    p.add_argument("--dry-run", action="store_true",
                   help="only report how many episodes are pending")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser(
        "load", help="load pre-computed extraction JSONL (batch runs, session output)"
    )
    p.add_argument("file", help="JSONL: {episode_id, topics: [{label, genres, confidence}]}")
    p.add_argument("--source", default="batch",
                   help="value stored in episode_topics.source (default: batch)")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser(
        "compare", help="compare cross-show transcripts for topics covered by 2+ shows"
    )
    p.add_argument("--limit", type=int, help="max topics to process this run")
    p.add_argument("--model", default=os.environ.get("HARK_CLAIMS_MODEL", claims.DEFAULT_MODEL),
                   help=f"Claude model id (default: $HARK_CLAIMS_MODEL or {claims.DEFAULT_MODEL})")
    p.add_argument("--dry-run", action="store_true",
                   help="only report how many topics are pending")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser(
        "load-comparisons",
        help="load pre-computed claims comparisons JSONL (batch runs, session output)",
    )
    p.add_argument(
        "file", help="JSONL: {topic_id, shared: [str], unique_by_show: {show: [str]}}"
    )
    p.add_argument("--source", default="session",
                   help="value stored in topic_comparisons.model (default: session)")
    p.set_defaults(func=cmd_load_comparisons)

    p = sub.add_parser("stats", help="print database counts per show")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("canon", help="retry Wikidata canonicalization for unmatched topics")
    p.set_defaults(func=cmd_canon)

    p = sub.add_parser(
        "web", help="serve the dashboard (login-walled) + feed/audio routes (token-gated)"
    )
    p.add_argument("--bind", default=os.environ.get("HARK_BIND", "0.0.0.0:8710"),
                   help="host:port (default: $HARK_BIND or 0.0.0.0:8710)")
    p.add_argument("--auth-db", default=os.environ.get("HARK_AUTH_DB", "auth.db"),
                   help="auth database path, kept separate from hark.db "
                        "(default: $HARK_AUTH_DB or auth.db)")
    p.add_argument("--base-url", default=os.environ.get("HARK_BASE_URL", "http://localhost:8710"),
                   help="externally-reachable URL this server is served at — embedded "
                        "in generated feeds' audio links, so it must resolve from "
                        "wherever the podcast player runs, not just from this host "
                        "(default: $HARK_BASE_URL or http://localhost:8710)")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("topics", help="list topics by cross-show coverage")
    p.add_argument("--limit", type=int, default=25, help="rows to show (default: 25)")
    p.set_defaults(func=cmd_topics)

    p = sub.add_parser("who", help="who covered a topic: search by label or QID")
    p.add_argument("topic", help="topic label substring or Wikidata QID")
    p.set_defaults(func=cmd_who)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
