"""hark command line: resolve, ingest, extract, stats, topics, who."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Callable

import httpx

from . import __version__, db, extract, ingest, pipeline, resolve, wikidata

DEFAULT_DB = os.environ.get("HARK_DB", "hark.db")
DEFAULT_FEEDS = "feeds.txt"
DEFAULT_MODEL = os.environ.get("HARK_MODEL", extract.DEFAULT_MODEL)
USER_AGENT = f"hark/{__version__} (homelab podcast indexer)"


def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=30.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )


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
    print(f"shows:    {shows['n']} ({shows['resolved']} resolved)")
    print(f"episodes: {episodes}")
    print(f"topics:   {topics} ({links} episode links)")
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

    p = sub.add_parser("stats", help="print database counts per show")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("canon", help="retry Wikidata canonicalization for unmatched topics")
    p.set_defaults(func=cmd_canon)

    p = sub.add_parser("web", help="serve the web frontend (login-walled)")
    p.add_argument("--bind", default=os.environ.get("HARK_BIND", "0.0.0.0:8710"),
                   help="host:port (default: $HARK_BIND or 0.0.0.0:8710)")
    p.add_argument("--auth-db", default=os.environ.get("HARK_AUTH_DB", "auth.db"),
                   help="auth database path, kept separate from hark.db "
                        "(default: $HARK_AUTH_DB or auth.db)")
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
