"""hark command line: resolve, ingest, sync-subscriptions, sync-history,
import-opml, discover, extract, chapters, transcribe, detect-ads,
load-ad-detections, cut, fsck, compare, load-comparisons, stats, topics, who,
rate-shows, web.

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
from adscrub import dai as ad_dai
from adscrub import detect as ad_detect
from adscrub import fingerprint as ad_fingerprint
from adscrub import repeats as ad_repeats
from adscrub import transcribe as ad_transcribe

from . import (
    __version__, claims, dai_probe, db, discover, extract, gpodder_server,
    hosting, ingest, llm_budget, nextcloud, opml, orchestrator, pipeline,
    ratings, resolve, wikidata,
)

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
    than re-querying it on every one of a command's 2-3 call sites.

    A slice, not a truthy check, on purpose: `filtered[:None]` already means
    "no limit" in Python, so this handles limit=0 correctly too (an empty
    result, matching `--limit 0`'s meaning everywhere else in this codebase)
    instead of a truthy `if limit else filtered` treating 0 as "unlimited"."""
    filtered = [e for e in episodes if e["show_id"] in enabled_ids]
    return filtered[:limit]


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
        # Nextcloud-client sync is inherently single-account (one set of
        # HARK_NEXTCLOUD_* creds, unlike the multi-account gpodder *server*
        # path in gpodder_server.py) — attribute to user 1, the bootstrap
        # account, same as this repo's other user_id-1 backfills.
        show = conn.execute("SELECT id FROM shows WHERE feed_url = ?", (feed_url,)).fetchone()
        if show is not None:
            conn.execute(
                "INSERT OR IGNORE INTO user_shows (user_id, show_id) VALUES (1, ?)", (show["id"],)
            )
    conn.commit()
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
    # Same storage path gpodder_server.py uses for actions AntennaPod posts
    # to hark directly — one place validates/inserts into listen_actions
    # regardless of which direction the data arrived from. user_id 1: see
    # the single-account note in cmd_sync_subscriptions above.
    inserted = gpodder_server.record_episode_actions(conn, 1, actions)
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


def cmd_repeats(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    enabled = _enabled_show_ids(conn)
    library = ad_repeats.build_library(conn)
    if not library:
        print("no confirmed ad spans yet — chapters or detect-ads has to go first",
              file=sys.stderr)
        return 1

    episodes = _filter_enabled(
        conn.execute(
            "SELECT * FROM episodes WHERE transcript_path IS NOT NULL ORDER BY id"
        ).fetchall(),
        enabled,
        args.limit,
    )
    if args.dry_run:
        print(f"{len(episodes)} transcribed episode(s) scannable against "
              f"{len(library):,} known ad shingles")
        return 0

    # Per-episode, like cmd_detect_ads, so the per-show ad_stripping_enabled filter above
    # actually takes effect — adscrub's bulk apply_repeats() does its own episode query
    # with no way to restrict it to a chosen set.
    detector = ad_repeats.RepeatAdDetector(library)
    found = hit = errors = 0
    for ep in episodes:
        try:
            n = ad_repeats.repeat_episode(conn, ep, detector)
        except Exception as exc:  # noqa: BLE001 — one bad transcript must not stop the sweep
            conn.rollback()
            errors += 1
            print(f"  FAIL  {ep['title']}: {exc}", file=sys.stderr)
            continue
        if n:
            hit += 1
            found += n
    print(f"matched {found} repeated ad span(s) across {hit} of {len(episodes)} episode(s) "
          f"({errors} failed) — {len(library):,} known ad shingles, no model called")
    return 0


def cmd_fingerprint(args: argparse.Namespace) -> int:
    """Match episode AUDIO against ad recordings already confirmed in this corpus.

    Costs no tokens and needs no transcript, so unlike detect-ads it can run while hark's
    Claude-driven pipeline stays disabled.
    """
    conn = db.connect(args.db)
    if not ad_fingerprint.fpcalc_available():
        print("fpcalc (Chromaprint / libchromaprint-tools) is not installed", file=sys.stderr)
        return 1
    enabled = _enabled_show_ids(conn)
    enabled_ids = [r["id"] for r in conn.execute(
        "SELECT id, show_id FROM episodes WHERE audio_url IS NOT NULL") if r["show_id"] in enabled]

    if args.index:
        # INDEX MODE: bounded fpcalc of un-indexed local audio (a shrinking pending queue). This
        # is the tier's one expensive step; the pipeline runs it a slice at a time so the
        # one-time backfill spreads across cycles instead of stalling. Matching (below) is cheap.
        r = ad_fingerprint.index_episodes(conn, episode_ids=enabled_ids, limit=args.limit)
        print(f"indexed {r.indexed} episode(s), {r.pending} still pending (fpcalc'd once, cached)")
        return 0

    def progress(n: int, total: int) -> None:
        if n == 1 or n % 50 == 0 or n == total:
            print(f"  ..    building library: fingerprinted {n}/{total} confirmed ad read(s)",
                  file=sys.stderr)

    library = ad_fingerprint.build_library(conn, on_progress=progress)
    if not library:
        print("no confirmed ad recordings yet — chapters or detect-ads has to go first",
              file=sys.stderr)
        return 1

    # LOCAL AUDIO ONLY, unless asked otherwise. adscrub's fingerprint_episode downloads an
    # episode whose audio isn't cached — correct for adscrub standalone, catastrophic here:
    # this corpus has ~27,800 episodes with an audio_url and only ~1,300 downloaded, so an
    # unguarded sweep would pull terabytes from podcast CDNs and fill the pool. hark manages
    # audio deliberately (transcribe downloads what it needs), so the default is to fingerprint
    # what is already on disk and leave acquisition to the tier that owns it.
    audio_dir = Path(os.environ.get("ADSCRUB_DATA_DIR", "data")) / "audio"
    rows = conn.execute("SELECT * FROM episodes WHERE audio_url IS NOT NULL ORDER BY id").fetchall()
    if not args.download:
        rows = [r for r in rows if (audio_dir / f"{r['id']}.mp3").exists()]
    episodes = _filter_enabled(rows, enabled, args.limit)
    if args.dry_run:
        print(f"{len(episodes)} episode(s) scannable against a library of "
              f"{library.n_episodes} source episode(s)")
        return 0

    # Per-episode rather than adscrub's bulk apply_fingerprints(), for the same reason as
    # cmd_repeats: the bulk helper runs its own episode query with no way to restrict it to
    # the ad-stripping-enabled shows selected above.
    # MATCH MODE (default): indexed_only, so it only touches episodes already fingerprinted —
    # no download, no fpcalc, cheap enough to run every cycle. An un-indexed episode is skipped
    # (returns -1), left to the bounded --index stage. `--download` restores the old
    # index-on-match behaviour for a manual full run.
    detector = ad_fingerprint.AudioFingerprintDetector(library)
    found = hit = errors = skipped = 0
    with make_client() as client:
        for ep in episodes:
            try:
                n = ad_fingerprint.fingerprint_episode(conn, ep, detector, client,
                                                        indexed_only=not args.download)
            except Exception as exc:  # noqa: BLE001 — one bad episode must not stop the sweep
                conn.rollback()
                errors += 1
                print(f"  FAIL  {ep['title'] or ''}: {exc}", file=sys.stderr)
                continue
            if n < 0:
                skipped += 1
                continue
            if n:
                hit += 1
                found += n
                print(f"  ok    {ep['title'] or ''}: {n} fingerprinted ad span(s)")
    tail = f", {skipped} not indexed yet" if skipped else ""
    print(f"matched {found} ad span(s) across {hit} of {len(episodes) - skipped} indexed "
          f"episode(s) ({errors} failed{tail}) — no transcript, no model")
    return 0


def cmd_discover_ads(args: argparse.Namespace) -> int:
    """Cold start for ONE show: find its ads by matching its episodes against each other.

    Scoped to a single show on purpose. Recurrence is measured against whatever set it is
    given, and hark holds ~70 unrelated feeds in one database — pooling them would build one
    enormous index and compute the stop-list over a corpus that shares no ad pool.
    """
    conn = db.connect(args.db)
    if not ad_fingerprint.fpcalc_available():
        print("fpcalc (Chromaprint / libchromaprint-tools) is not installed", file=sys.stderr)
        return 1
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM episodes WHERE show_id = ? AND audio_url IS NOT NULL", (args.show,))]
    if len(ids) < ad_fingerprint.RECUR_MIN_EPISODES:
        print(f"show {args.show} has {len(ids)} episode(s) with audio; need at least "
              f"{ad_fingerprint.RECUR_MIN_EPISODES} to tell recurring audio from content",
              file=sys.stderr)
        return 1

    def report(r: ad_fingerprint.DiscoverResult) -> None:
        if r.error:
            print(f"  FAIL  {r.title}: {r.error}", file=sys.stderr)
        elif r.found:
            print(f"  ok    {r.title}: {r.found} recurring region(s)")

    results = ad_fingerprint.discover_recurring(
        conn, limit=args.limit, on_result=report, episode_ids=ids)
    found = sum(r.found for r in results)
    hit = sum(1 for r in results if r.found)
    print(f"found {found} recurring region(s) across {hit} of {len(results)} episode(s) — "
          "no library, no transcript, no model. These are INFERENCE (source='recur') and are "
          "NOT cut by default; roughly 1 in 10 may not be an ad.")
    return 0


def _estimate_episode_dollars(ep) -> float:
    """Conservative $ estimate for reading one episode: the whole transcript's character count
    priced as input tokens. Ignores that covered regions are omitted (an overestimate), which is
    the safe direction for a budget cap — it stops sooner, never later."""
    import json
    try:
        with open(ep["transcript_path"], encoding="utf-8") as fh:
            data = json.load(fh)
        segs = data.get("segments", data) if isinstance(data, dict) else data
        chars = sum(len(s.get("text", "")) for s in segs)
    except (OSError, json.JSONDecodeError, TypeError):
        chars = 0
    return llm_budget.estimate_dollars(chars)


def cmd_pipeline(args: argparse.Namespace) -> int:
    """Run the whole ad/topic pipeline: ingest -> transcribe -> fingerprint -> repeats -> cut,
    plus the drop-file loads, and (with a key + budget) detect-ads. See orchestrator.py.

    `--once` runs a single pass and exits (what tests and a cron would call); the default loops.
    Free stages always run; LLM stages are gated on ANTHROPIC_API_KEY and HARK_LLM_DAILY_BUDGET,
    so nothing spends until both are set."""
    if args.once:
        for name, outcome in orchestrator.run_cycle(args.db):
            print(f"  {outcome:16} {name}")
        return 0
    orchestrator.run_loop(args.db, interval=args.interval)
    return 0


def cmd_detect_ads(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    enabled = _enabled_show_ids(conn)
    all_pending = _filter_enabled(ad_detect.pending_episodes(conn), enabled)

    # WHAT IS ACTUALLY OWED IS CAMPAIGNS, NOT EPISODES.
    #
    # `pending_episodes` counts every transcribed episode the model has not read — a definition
    # written before there was any way to tell which of them were worth reading. It is what
    # manufactures a 1,262-episode "backlog" on this corpus, and reading it would be both
    # expensive and largely redundant: twelve episodes carrying one sponsor read are one thing
    # to learn, not twelve, and the fingerprint tier recognises the other eleven for free once
    # any one of them is confirmed.
    #
    # So the default queue is now the set-cover over UNREAD campaigns: the fewest episodes that
    # confirm every ad recording the library does not already know. It shrinks as campaigns are
    # confirmed and grows only when a genuinely new one appears. `--all-pending` restores the
    # old episode-wise sweep. Nothing is marked processed and no episode is retired — an episode
    # not selected simply is not needed yet.
    seeds = []
    if not args.all_pending and all_pending:
        seeds = ad_fingerprint.select_seed_episodes(
            conn, episode_ids=sorted(e["id"] for e in all_pending), limit=args.limit)

    if seeds:
        by_id = {e["id"]: e for e in all_pending}
        pending = [by_id[eid] for eid, _ in seeds if eid in by_id]
        scope = (f"{len(seeds)} episode(s) covering every unread campaign "
                 f"(of {len(all_pending)} unread episodes)")
    else:
        # No campaigns to cover — either --all-pending, or the corpus has too little downloaded
        # audio for self-recurrence to say anything (see fingerprint.RECUR_MIN_EPISODES). Fall
        # back to the episode-wise sweep rather than silently doing nothing: without audio there
        # is no cheaper option, and reading nothing would leave ads in the feed.
        ranked = ad_repeats.prioritize_pending(conn, all_pending, group_column="show_id")
        pending = ranked[: args.limit]
        scope = f"{len(all_pending)} pending episode(s) (no campaign data — episode-wise)"

    if args.dry_run:
        print(f"to read: {scope}")
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
    # Daily budget: when a cap is set ($HARK_LLM_DAILY_BUDGET) the loop stops before the next
    # episode once today's spend would exceed it, so 'auto-enabled' can't run away. With no cap
    # set, remaining() is 0 -> this command still runs when invoked by hand, but the PIPELINE
    # never schedules it (its needs_budget gate skips it). Spend is estimated from the transcript
    # actually sent, not read back from the API — enough to STOP in time.
    cap = llm_budget.daily_cap()
    ok = errors = 0
    consecutive_errors = 0
    max_consecutive_errors = 5
    for ep in pending:
        if cap and llm_budget.remaining(conn) <= 0:
            print(f"  stop  daily LLM budget ${cap:.2f} reached "
                  f"(spent ${llm_budget.spent_today(conn):.2f}); {ep['title'] or ''} deferred")
            break
        try:
            spend = _estimate_episode_dollars(ep)
            found = ad_detect.detect_episode(conn, ep, detector)
            if cap:
                llm_budget.record(conn, spend)
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


class _PrecomputedDetector:
    """Adapts session-as-X-produced {start_segment, end_segment, reason} spans
    for ONE episode to the AdSpanDetector protocol, so detect_episode() below
    runs unchanged — same index-grounding (adscrub's spans_from_segment_indices)
    and llm_detected_at marking a live ClaudeAdDetector run gets, just fed
    pre-computed judgment instead of calling the API. Mirrors extract/compare's
    own load-precomputed idiom (pipeline.load_extractions/claims.load_comparisons).

    Also accepts a bare [start_segment, end_segment] pair per span (no
    `reason`) — a real fleet-agent batch dropped in production on 2026-07-13
    used this shorthand instead of the documented dict shape, and
    spans_from_segment_indices() (adscrub, dict-only) would otherwise crash
    with 'list' object has no attribute 'get' on every record in the batch."""

    def __init__(self, raw_spans: list):
        self._raw_spans = raw_spans

    def detect(
        self, transcript: list[dict], skip: frozenset[int] = frozenset()
    ) -> list[ad_detect.DetectedAdSpan]:
        # `skip` (adscrub >= 0.8.0) lets a per-token tier ignore transcript a cheaper tier
        # already covered. Ignored here for the same reason repeats ignores it: these spans are
        # already computed and cost nothing to return, so looking away could only lose coverage.
        normalized = []
        for raw in self._raw_spans:
            if isinstance(raw, dict):
                normalized.append(raw)
            elif isinstance(raw, (list, tuple)) and len(raw) == 2:
                normalized.append({"start_segment": raw[0], "end_segment": raw[1]})
            # else: left out — spans_from_segment_indices already drops
            # missing/out-of-range indices the same way, this just extends
            # that tolerance to a shape it can't itself introspect.
        return ad_detect.spans_from_segment_indices(transcript, normalized)


def cmd_seeds(args: argparse.Namespace) -> int:
    """Emit the transcripts a reader must see to confirm every unread campaign.

    This is the subscription-path twin of `detect-ads`. Both answer "what is worth reading?"
    with the same campaign set cover; they differ only in who does the reading — the API path
    calls a model from inside the container, this one hands the material to a Claude Code
    session, which writes the result back through `load-ad-detections`.

    Segments are rendered with adscrub's OWN chunk renderer rather than a hark-side copy, so
    the indices a reader points at are the exact indices `spans_from_segment_indices` will
    ground against on the way back in. Regions an earlier tier already covered are omitted,
    for the same reason the API path omits them: nobody should be paid to re-read them.
    """
    import json

    conn = db.connect(args.db)
    enabled = _enabled_show_ids(conn)
    all_pending = _filter_enabled(ad_detect.pending_episodes(conn), enabled)
    if not all_pending:
        print("nothing unread on an ad-stripping-enabled show", file=sys.stderr)
        return 1
    seeds = ad_fingerprint.select_seed_episodes(
        conn, episode_ids=sorted(e["id"] for e in all_pending), limit=args.limit)
    if not seeds:
        print("no unread campaigns — every recording found is already confirmed, or there is "
              "too little downloaded audio for self-recurrence to say anything", file=sys.stderr)
        return 1

    by_id = {e["id"]: e for e in all_pending}
    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    try:
        print(f"# {len(seeds)} episode(s) to read — chosen to cover every unread ad campaign.",
              file=out)
        print("# For each, identify contiguous runs of segments that are advertising, not"
              " editorial.", file=out)
        print("# Write one JSON object per line to the drop file, then run"
              " `hark load-ad-detections <file>`:", file=out)
        print('#   {"episode_id": N, "ad_spans": [{"start_segment": i, "end_segment": j,'
              ' "reason": "..."}]}', file=out)
        print("# Indices are the [n] markers below and are GLOBAL — an omitted run is noted"
              " inline.", file=out)
        for eid, covers in seeds:
            row = by_id[eid]
            try:
                with open(row["transcript_path"], encoding="utf-8") as fh:
                    tx = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"  (skipped episode {eid}: {exc})", file=sys.stderr)
                continue
            segs = tx["segments"] if isinstance(tx, dict) else tx
            skip = ad_detect.covered_segment_indices(conn, eid, segs)
            print(f"\n{'=' * 70}\n# episode_id={eid}  covers {covers} unread campaign(s)"
                  f"\n# {row['title'] or ''}\n{'=' * 70}", file=out)
            for chunk in ad_detect._chunks(segs, skip):
                print(chunk, file=out)
    finally:
        if args.out:
            out.close()
            print(f"wrote {len(seeds)} episode(s) to {args.out}", file=sys.stderr)
    return 0


def cmd_load_ad_detections(args: argparse.Namespace) -> int:
    import json

    conn = db.connect(args.db)
    try:
        with open(args.file, encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 1

    ok = failed = 0
    for rec in records:
        episode_id = rec.get("episode_id")
        row = conn.execute(
            "SELECT * FROM episodes WHERE id = ? AND transcript_path IS NOT NULL", (episode_id,)
        ).fetchone() if episode_id is not None else None
        if row is None:
            failed += 1
            print(f"  FAIL  episode {episode_id!r}: no such episode with a transcript")
            continue
        try:
            found = ad_detect.detect_episode(
                conn, row, _PrecomputedDetector(rec.get("ad_spans", []))
            )
        except Exception as exc:  # noqa: BLE001 — per-record isolation, matches cmd_load
            conn.rollback()
            failed += 1
            print(f"  FAIL  {row['title'] or ''}: {exc}")
            continue
        ok += 1
        print(f"  ok    {row['title'] or ''}: {found} ad span(s) loaded")
    print(f"loaded {ok} episode(s) ({failed} failed)")
    return 1 if failed else 0


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
        results = pipeline.recanonicalize(conn, canon.canonicalize, limit=args.limit)
    for r in results:
        action = "merged into" if r.merged else "->"
        print(f"  {r.old_label} {action} {r.new_label} [{r.qid}]")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM topics WHERE wikidata_id IS NULL"
    ).fetchone()[0]
    print(f"canonicalized {len(results)} topics ({remaining} still unmatched)")
    return 0


def cmd_rate_shows(args: argparse.Namespace) -> int:
    """M4: enrich shows with data scoring.py's recommendations (see
    /notable) draw on. Two independent steps, each isolated per-show:
    itunes_id backfill (resolve.py — free, keyless, runs regardless) then
    external show ratings (ratings.py — needs $HARK_TADDY_USER_ID and
    $HARK_TADDY_API_KEY; runs with a NullRatingsSource, a no-op, if either
    is unset). --limit caps each step independently, not a combined total."""
    conn = db.connect(args.db)
    with make_client() as client:
        backfilled = resolve.backfill_itunes_ids(conn, client, limit=args.limit)
    matched = sum(1 for r in backfilled if r.itunes_id is not None)
    print(f"itunes_id backfill: {matched}/{len(backfilled)} newly matched")

    taddy_user_id = os.environ.get("HARK_TADDY_USER_ID")
    taddy_api_key = os.environ.get("HARK_TADDY_API_KEY")
    if not taddy_user_id or not taddy_api_key:
        print("hint: set $HARK_TADDY_USER_ID and $HARK_TADDY_API_KEY to fetch external show "
              "ratings (free, no card — see taddy.org/developers) — skipping the ratings refresh",
              file=sys.stderr)
        return 0

    with make_client() as client:
        source = ratings.TaddyRatingsSource(client, taddy_user_id, taddy_api_key)
        results = ratings.refresh_ratings(conn, source, limit=args.limit)
    errors = 0
    for r in results:
        if r.error:
            errors += 1
            print(f"  FAIL  {r.query}: {r.error}")
        elif r.rating is None:
            print(f"  miss  {r.query}: not found on Taddy")
        elif r.rating.rating_avg is None:
            print(f"  ok    {r.query}: found, but outside any popularity tier")
        else:
            print(f"  ok    {r.query}: score {r.rating.rating_avg:.2f} "
                  f"(confidence {r.rating.rating_count})")
    print(f"refreshed ratings for {len(results) - errors} show(s) ({errors} failed)")
    return 1 if errors else 0


def cmd_dai_probe(args: argparse.Namespace) -> int:
    """Research (2026-07-14): does re-fetching an episode's audio with different
    request signals (User-Agent) return genuinely different content? If so, the
    divergence point is an ad boundary found with zero transcription/LLM calls.
    Backfills shows.hosting_platform first (free, keyless), then probes up to
    --per-platform episodes per distinct platform that haven't reached
    --min-trials attempts yet, so results can show which platforms actually
    support the technique rather than just running it once. A single probe is
    not a reliable verdict — see select_sample()'s own docstring for the
    real-data example — so this command is meant to be run periodically (a
    scheduled job), not once, to actually accumulate --min-trials per episode.
    See adscrub.dai's own module docstring for what the technique can and
    can't do."""
    conn = db.connect(args.db)
    backfilled = hosting.backfill_hosting_platform(conn)
    print(f"hosting_platform backfill: {backfilled} show(s) newly classified")

    sample = dai_probe.select_sample(
        conn, per_platform=args.per_platform, limit=args.limit, min_trials=args.min_trials
    )
    if args.dry_run:
        by_platform: dict[str, int] = {}
        for ep in sample:
            by_platform[ep["hosting_platform"]] = by_platform.get(ep["hosting_platform"], 0) + 1
        print(f"would probe {len(sample)} episode(s) across {len(by_platform)} platform(s):")
        for platform, n in sorted(by_platform.items()):
            print(f"  {platform}: {n}")
        return 0
    if not sample:
        print("no untested episodes with a known hosting_platform", file=sys.stderr)
        return 1

    errors = 0
    stored = 0
    # A fresh client per fetch, not one shared client for the whole run — see
    # adscrub.dai's own docstring for why a shared client's cookie jar
    # silently defeats the comparison. User-Agent is set per-fetch by
    # probe_variance() itself, so no default is needed here.
    client_factory = lambda: httpx.Client(timeout=60.0)  # noqa: E731
    for ep in sample:
        r = dai_probe.run_probe(client_factory, conn, ep, ep["hosting_platform"])
        if r.error or r.result is None:  # run_probe sets exactly one of the two
            errors += 1
            print(f"  FAIL  [{r.platform}] {r.title}: {r.error}")
        elif not r.result.diverged:
            print(f"  same  [{r.platform}] {r.title}: no divergence in "
                  f"{r.result.bytes_compared} bytes compared")
        else:
            reconv = (f", reconverges at byte {r.result.reconvergence_byte}"
                      if r.result.reconverged else ", no reconvergence found")
            print(f"  DIFF  [{r.platform}] {r.title}: diverges at byte "
                  f"{r.result.divergence_byte}{reconv}")
            # Persist what the probe already found, rather than re-probing. A stored `dai` span
            # seeds the fingerprint library for free (source='dai' is in FP_LIBRARY_SOURCES,
            # not in CUT_SOURCES) — it only fires when the episode's audio is on disk to convert
            # the probe's byte offsets to seconds, so it's silent, not failed, otherwise.
            store = ad_dai.store_probe_result(conn, ep, r.result)
            stored += store.stored
    tail = f", {stored} stored as dai span(s)" if stored else ""
    print(f"probed {len(sample) - errors} episode(s) ({errors} failed){tail}")

    print()
    print("per-platform summary (all probes ever run):")
    for row in dai_probe.platform_summary(conn):
        print(f"  {row['platform']}: {row['diverged']}/{row['tested']} diverged, "
              f"{row['reconverged']} of those also reconverged")
    return 1 if errors else 0


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


def _open_auth(args: argparse.Namespace):
    from . import web  # deferred: most commands don't need the web module at all

    # admin_token=None: these commands only touch the users table directly,
    # never verify()'s bootstrap-token login path, so there's nothing for a
    # token to gate here.
    return web.Auth(args.auth_db, admin_token=None)


def cmd_user_add(args: argparse.Namespace) -> int:
    auth = _open_auth(args)
    try:
        auth.create_user(args.username, is_admin=args.admin)
    except Exception as exc:  # noqa: BLE001 — surfaces e.g. UNIQUE violation on a dup username
        print(f"cannot create {args.username!r}: {exc}", file=sys.stderr)
        return 1
    print(f"created {args.username!r}{' (admin)' if args.admin else ''} — "
          f"no password set yet; log in once with $HARK_ADMIN_TOKEN and set one at /account")
    return 0


def cmd_user_list(args: argparse.Namespace) -> int:
    auth = _open_auth(args)
    users = auth.list_users()
    if not users:
        print("no users", file=sys.stderr)
        return 1
    for u in users:
        flags = " ".join(f for f, on in (
            ("admin", u["is_admin"]), ("has-password", u["has_password"]),
        ) if on)
        print(f"  {u['id']:<4} {u['username']:<20} {flags}")
        if u["invite_pending"]:
            print(f"        invite pending: /invite/{u['invite_token']}")
    return 0


def cmd_user_remove(args: argparse.Namespace) -> int:
    auth = _open_auth(args)
    if not auth.delete_user(args.username):
        print(f"no such user {args.username!r}", file=sys.stderr)
        return 1
    print(f"removed {args.username!r} (sessions revoked; their show subscriptions are untouched"
          f" in hark.db — see docs/PLAN.md if you want those cleared too)")
    return 0


def cmd_user_invite(args: argparse.Namespace) -> int:
    """Preferred over `user add` for onboarding someone else: a single-use
    link scoped to just their account, instead of handing out the shared
    $HARK_ADMIN_TOKEN (which also happens to work on any other
    as-yet-passwordless row)."""
    auth = _open_auth(args)
    try:
        _, token = auth.create_invite(args.username, is_admin=args.admin)
    except Exception as exc:  # noqa: BLE001 — surfaces e.g. UNIQUE violation on a dup username
        print(f"cannot invite {args.username!r}: {exc}", file=sys.stderr)
        return 1
    path = f"/invite/{token}"
    from . import web

    # Precedence: an explicit --base-url is deliberate intent and wins
    # outright; otherwise prefer whatever an admin has set from
    # /admin/users (auth.get_setting), since this command doesn't run
    # against a live App instance that would already resolve that itself;
    # $HARK_BASE_URL is the last, deploy-time fallback.
    base = args.base_url or auth.get_setting(web.BASE_URL_SETTING) or os.environ.get("HARK_BASE_URL", "")
    link = f"{base.rstrip('/')}{path}" if base else path

    print(f"invited {args.username!r}{' (admin)' if args.admin else ''} — send them this link"
          f" (expires in {web.INVITE_EXPIRES_DAYS} days):")
    print(f"  {link}")
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

    p = sub.add_parser("repeats",
                       help="match transcripts against ad reads already confirmed elsewhere (free)")
    p.add_argument("--limit", type=int,
                   help="scan only the first N episodes by id — ad-hoc/testing only. There is no pending-queue (re-scanning is free and the library grows), so the pipeline runs this UNBOUNDED; a limit in a loop would rescan the same head forever and never reach the tail.")
    p.add_argument("--dry-run", action="store_true", help="only report what would be scanned")
    p.set_defaults(func=cmd_repeats)

    p = sub.add_parser(
        "fingerprint",
        help="match episode audio against known ad recordings (free, no transcript or model)")
    p.add_argument("--limit", type=int, help="max episodes to scan this run")
    p.add_argument("--index", action="store_true",
                   help="INDEX mode: fpcalc up to --limit un-indexed local-audio episodes and "
                        "cache them (a shrinking pending queue), instead of matching. This is "
                        "the tier's one expensive step; the pipeline runs it a slice per cycle.")
    p.add_argument("--download", action="store_true",
                   help="match episodes whose audio isn't on disk yet, downloading + fpcalc'ing "
                        "them inline (the old index-on-match behaviour). OFF by default: the "
                        "default match is indexed-only (cache hits, no download); ~27,800 "
                        "episodes have an audio_url and only ~1,300 are downloaded, so an "
                        "unguarded sweep would pull terabytes from podcast CDNs.")
    p.add_argument("--dry-run", action="store_true", help="only report how many are scannable")
    p.set_defaults(func=cmd_fingerprint)

    p = sub.add_parser(
        "discover-ads",
        help="cold start for one show: find ads by matching its episodes against each other")
    p.add_argument("--show", type=int, required=True, help="show id to scan")
    p.add_argument("--limit", type=int, help="max episodes to consider")
    p.set_defaults(func=cmd_discover_ads)

    p = sub.add_parser(
        "pipeline",
        help="run the whole ad/topic pipeline on a loop (the container's entrypoint). Free "
             "stages always run; LLM stages need ANTHROPIC_API_KEY + HARK_LLM_DAILY_BUDGET.")
    p.add_argument("--once", action="store_true", help="run one pass and exit (cron/test)")
    p.add_argument("--interval", type=float, default=60.0, help="seconds between passes (default 60)")
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("detect-ads", help="classify ad spans from transcripts with a Claude model")
    p.add_argument("--all-pending", action="store_true",
                   help="read every unread episode instead of the fewest episodes covering all "
                        "unread campaigns. The default is campaign set-cover: reading one episode "
                        "of a campaign teaches the library the rest, so an episode-wise sweep "
                        "mostly re-reads what fingerprinting already recognises for free.")
    p.add_argument("--limit", type=int, help="max episodes to process this run")
    p.add_argument("--model", default=os.environ.get("HARK_AD_MODEL", ad_detect.DEFAULT_MODEL),
                   help=f"Claude model id (default: $HARK_AD_MODEL or {ad_detect.DEFAULT_MODEL})")
    p.add_argument("--dry-run", action="store_true",
                   help="only report how many episodes are pending")
    p.set_defaults(func=cmd_detect_ads)

    p = sub.add_parser(
        "seeds",
        help="emit the transcripts worth reading to confirm every unread ad campaign "
             "(subscription path: read these, then load-ad-detections the result)",
    )
    p.add_argument("--limit", type=int, help="max episodes to emit")
    p.add_argument("--out", help="write to a file instead of stdout")
    p.set_defaults(func=cmd_seeds)

    p = sub.add_parser(
        "load-ad-detections",
        help="load pre-computed ad-span detections JSONL (batch runs, session output)",
    )
    p.add_argument(
        "file",
        help="JSONL: {episode_id, ad_spans: [{start_segment, end_segment, reason}]}",
    )
    p.set_defaults(func=cmd_load_ad_detections)

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
    p.add_argument("--limit", type=int, help="max unmatched topics to attempt this run")
    p.set_defaults(func=cmd_canon)

    p = sub.add_parser(
        "rate-shows",
        help="M4: backfill itunes_id + refresh external show ratings (feeds /notable's recommendations)",
    )
    p.add_argument("--limit", type=int, help="max shows to process per step this run")
    p.set_defaults(func=cmd_rate_shows)

    p = sub.add_parser(
        "dai-probe",
        help="research: probe whether re-fetching audio with different signals reveals "
             "dynamic ad insertion, per hosting platform",
    )
    p.add_argument("--per-platform", type=int, default=1,
                    help="max episodes to probe per distinct hosting platform this run (default: 1)")
    p.add_argument("--limit", type=int, help="cap total episodes probed this run")
    p.add_argument("--min-trials", type=int, default=dai_probe.DEFAULT_MIN_TRIALS,
                    help=f"attempts an episode needs before it stops being resampled "
                         f"(default: {dai_probe.DEFAULT_MIN_TRIALS}) — a single probe isn't a "
                         f"reliable verdict, run this command periodically to reach it")
    p.add_argument("--dry-run", action="store_true", help="show what would be probed, fetch nothing")
    p.set_defaults(func=cmd_dai_probe)

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

    def _add_auth_db_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--auth-db", default=os.environ.get("HARK_AUTH_DB", "auth.db"),
                       help="auth database path (default: $HARK_AUTH_DB or auth.db)")

    p_user = sub.add_parser("user", help="manage accounts (auth.db) — multi-user subscriptions")
    user_sub = p_user.add_subparsers(dest="user_command", required=True)

    p = user_sub.add_parser(
        "add", help="create an account bootstrapped via the shared $HARK_ADMIN_TOKEN"
    )
    p.add_argument("username")
    p.add_argument("--admin", action="store_true",
                   help="grant admin (global show toggles, user management)")
    _add_auth_db_arg(p)
    p.set_defaults(func=cmd_user_add)

    p = user_sub.add_parser(
        "invite", help="create an account with a single-use invite link (preferred for onboarding)"
    )
    p.add_argument("username")
    p.add_argument("--admin", action="store_true", help="grant admin")
    p.add_argument("--base-url", default=None,
                   help="prepended to the printed invite path (default: $HARK_BASE_URL, "
                        "or just the bare /invite/<token> path if unset)")
    _add_auth_db_arg(p)
    p.set_defaults(func=cmd_user_invite)

    p = user_sub.add_parser("list", help="list accounts")
    _add_auth_db_arg(p)
    p.set_defaults(func=cmd_user_list)

    p = user_sub.add_parser("remove", help="delete an account (revokes its sessions)")
    p.add_argument("username")
    _add_auth_db_arg(p)
    p.set_defaults(func=cmd_user_remove)

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
