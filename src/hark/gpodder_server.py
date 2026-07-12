"""hark AS a GPodder Sync server — the reverse direction of nextcloud.py
(which is hark acting as a *client* reading Nextcloud's copy of this same
protocol). Implements exactly the four endpoints AntennaPod's own
NextcloudSyncService.java calls, confirmed against AntennaPod's actual
source (github.com/AntennaPod/AntennaPod, net/sync/gpoddernet) rather than
guessed from the server side — its login()/logout() are no-ops (no
Nextcloud-specific handshake to fake), so pointing AntennaPod's existing
"Nextcloud" sync setting at hark directly is enough; no app fork needed for
the sync half of M3.

Timestamp formats matter here and differ from the rest of hark:
- The protocol's `since`/response `timestamp` cursor is Unix epoch seconds
  (an int), not hark's usual ISO8601 strings.
- Individual action timestamps use `yyyy-MM-dd'T'HH:mm:ss` in UTC — no
  trailing `Z` or offset. AntennaPod's own parser (Java SimpleDateFormat)
  fails on the trailing `Z` hark.db.utcnow() normally appends, so responses
  from this module use their own formatter instead of utcnow().
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from . import resolve

_ACTION_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"
VALID_ACTIONS = {"new", "download", "play", "delete"}


def _format_ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(_ACTION_TS_FORMAT)


def record_subscription_changes(
    conn: sqlite3.Connection, add: list[str], remove: list[str]
) -> int:
    """Store a subscription_change/create upload and register any new feed
    URL (resolve.add_show_by_feed_url — same unreviewed-by-default path as
    sync-subscriptions/import-opml/discover). Never removes a show for a
    "remove" entry, matching hark's existing never-delete-on-unsubscribe
    stance — only the change event itself is recorded, for since= replay.
    Returns the epoch-second cursor to report back to the client."""
    now = int(time.time())
    for feed_url in add:
        conn.execute(
            "INSERT INTO subscription_changes (feed_url, action, occurred_at) VALUES (?, 'add', ?)",
            (feed_url, now),
        )
        resolve.add_show_by_feed_url(conn, feed_url)
    for feed_url in remove:
        conn.execute(
            "INSERT INTO subscription_changes (feed_url, action, occurred_at) VALUES (?, 'remove', ?)",
            (feed_url, now),
        )
    conn.commit()
    return now


def subscription_changes_since(
    conn: sqlite3.Connection, since: int
) -> tuple[list[str], list[str], int]:
    """add/remove feed URLs changed after `since` (epoch seconds), plus the
    cursor to report as this response's own `timestamp`. `since=0` naturally
    returns the full history, matching a first-ever sync."""
    rows = conn.execute(
        "SELECT feed_url, action FROM subscription_changes WHERE occurred_at > ? ORDER BY id",
        (since,),
    ).fetchall()
    add = [r["feed_url"] for r in rows if r["action"] == "add"]
    remove = [r["feed_url"] for r in rows if r["action"] == "remove"]
    return add, remove, int(time.time())


def record_episode_actions(conn: sqlite3.Connection, actions: list[dict]) -> int:
    """Store an episode_action/create upload into listen_actions — same
    table nextcloud-sync's sync-history populates, so both directions feed
    the same M4-bound data. Mandatory fields per AntennaPod's own
    EpisodeAction.readFromJsonObject: podcast, episode, action (one of
    VALID_ACTIONS); anything missing those is silently dropped, matching
    that method's own null-return-means-skip behavior rather than erroring
    the whole batch."""
    inserted = 0
    for a in actions:
        podcast, episode = a.get("podcast"), a.get("episode")
        action = (a.get("action") or "").lower()
        if not podcast or not episode or action not in VALID_ACTIONS:
            continue
        occurred_at = a.get("timestamp") or _format_ts(time.time())
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO listen_actions
                (podcast_url, episode_url, episode_guid, action, started, position, total, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (podcast, episode, a.get("guid"), action, a.get("started"), a.get("position"),
             a.get("total"), occurred_at),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def episode_actions_since(conn: sqlite3.Connection, since: int) -> tuple[list[dict], int]:
    """Actions recorded after `since` (epoch seconds), shaped exactly as
    AntennaPod's EpisodeAction.readFromJsonObject expects, plus the cursor
    to report as this response's own `timestamp`."""
    rows = conn.execute(
        """
        SELECT podcast_url, episode_url, episode_guid, action, started, position, total, occurred_at
        FROM listen_actions
        WHERE CAST(strftime('%s', occurred_at) AS INTEGER) > ?
        ORDER BY id
        """,
        (since,),
    ).fetchall()
    actions = []
    for r in rows:
        entry = {"podcast": r["podcast_url"], "episode": r["episode_url"], "action": r["action"]}
        if r["episode_guid"]:
            entry["guid"] = r["episode_guid"]
        if r["occurred_at"]:
            entry["timestamp"] = r["occurred_at"]
        # AntennaPod's own reader requires started>=0 and position/total>0 to
        # accept these fields at all for a PLAY action (readFromJsonObject) —
        # rows from before `started` existed (pre-0.12.0) have it NULL, so
        # only emit the trio when we genuinely have all three.
        if (r["action"] == "play" and r["started"] is not None
                and r["position"] is not None and r["total"] is not None):
            entry["started"] = r["started"]
            entry["position"] = r["position"]
            entry["total"] = r["total"]
        actions.append(entry)
    return actions, int(time.time())
