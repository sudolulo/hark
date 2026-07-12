"""M3: read (never write) AntennaPod's subscriptions + listen history from
Nextcloud's GPodder Sync app — the same account AntennaPod itself syncs to.

This is how new shows are meant to reach hark without hand-maintaining
feeds.txt (the manual `hark resolve` flow stays as a fallback — see
CLAUDE.md), and how per-episode listen history becomes available for M4's
"calibrated against the owner's actual listening" scoring later.

GPodder Sync's own API (github.com/thrillfall/nextcloud-gpodder) is a
partial implementation of the gpodder.net protocol; the two endpoints hark
needs:
  GET /index.php/apps/gpoddersync/subscriptions
      -> {"add": [url, ...], "remove": [url, ...], "timestamp": int}
      Full history of subscribe/unsubscribe events, not a live snapshot —
      "currently subscribed" is (add set) - (remove set). Cheap enough
      (tens of URLs) to always fetch in full; no cursor needed.
  GET /index.php/apps/gpoddersync/episode_action?since=<timestamp>
      -> {"actions": [{...}, ...], "timestamp": int}
      Play/skip/etc. events since `since` (0 for "everything"). This one
      does need a cursor — thousands of actions and growing — see
      sync_state's "gpodder_episode_action_since" key.
"""

from __future__ import annotations

import httpx


def current_subscriptions(client: httpx.Client, base_url: str, auth: tuple[str, str]) -> list[str]:
    """Feed URLs currently subscribed, per the full add/remove history."""
    resp = client.get(f"{base_url}/index.php/apps/gpoddersync/subscriptions", auth=auth)
    resp.raise_for_status()
    data = resp.json()
    return sorted(set(data.get("add", [])) - set(data.get("remove", [])))


def fetch_episode_actions(
    client: httpx.Client, base_url: str, auth: tuple[str, str], since: int = 0
) -> tuple[list[dict], int]:
    """Episode actions since the given cursor, and the cursor to store for
    next time. `since=0` fetches everything GPodder Sync has recorded."""
    resp = client.get(
        f"{base_url}/index.php/apps/gpoddersync/episode_action",
        params={"since": since},
        auth=auth,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("actions", []), data.get("timestamp", since)
