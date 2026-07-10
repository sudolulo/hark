"""hark command line: resolve, ingest, stats."""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from . import __version__, db, ingest, resolve

DEFAULT_DB = os.environ.get("HARK_DB", "hark.db")
DEFAULT_FEEDS = "feeds.txt"
USER_AGENT = f"hark/{__version__} (homelab podcast indexer)"


def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=30.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )


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

    p = sub.add_parser("stats", help="print database counts per show")
    p.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
