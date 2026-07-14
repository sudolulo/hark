"""Select and run dual-fetch DAI probes (adscrub.dai.probe_variance), and
persist results into dai_probes so results can be compared per
shows.hosting_platform (see hosting.py) — the whole point is finding out
which platforms actually support this technique, not just running it once.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

import httpx
from adscrub import dai

from .db import utcnow


@dataclass
class ProbeResult:
    episode_id: int
    title: str
    platform: str | None
    result: dai.DAIProbeResult | None = None
    error: str | None = None


def select_sample(
    conn: sqlite3.Connection, per_platform: int = 1, limit: int | None = None
) -> list[sqlite3.Row]:
    """Pick up to `per_platform` untested episodes from each distinct
    hosting_platform, oldest-tested-first within a platform (so a platform
    already probed doesn't keep getting picked while others go untested).
    Shows with no hosting_platform yet are skipped — run
    hosting.backfill_hosting_platform() first."""
    rows = conn.execute(
        """
        SELECT e.*, s.hosting_platform,
               (SELECT COUNT(*) FROM dai_probes p WHERE p.episode_id = e.id) AS probe_count
        FROM episodes e
        JOIN shows s ON s.id = e.show_id
        WHERE e.audio_url IS NOT NULL AND s.hosting_platform IS NOT NULL
        ORDER BY probe_count ASC, e.id ASC
        """
    ).fetchall()
    per_platform_count: dict[str, int] = {}
    sample = []
    for row in rows:
        platform = row["hosting_platform"]
        if row["probe_count"] > 0:
            continue  # already probed at least once; a fresh platform is more useful
        if per_platform_count.get(platform, 0) >= per_platform:
            continue
        per_platform_count[platform] = per_platform_count.get(platform, 0) + 1
        sample.append(row)
        if limit is not None and len(sample) >= limit:
            break
    return sample


def run_probe(
    client_factory: Callable[[], httpx.Client],
    conn: sqlite3.Connection,
    episode: sqlite3.Row,
    platform: str | None,
) -> ProbeResult:
    """Probe one episode and store the result (an attempt is always recorded,
    even a failure, so a broken/unreachable URL doesn't get retried forever).
    `platform` is passed explicitly rather than read off `episode` — a plain
    `episodes` row has no `hosting_platform` column, only a row joined against
    `shows` (like select_sample()'s) does, and this shouldn't assume its caller
    used that join. `client_factory` is forwarded straight to
    adscrub.dai.probe_variance() — see its own docstring for why each fetch
    needs an independently-constructed client, not a shared one."""
    try:
        result = dai.probe_variance(client_factory, episode["audio_url"])
    except (httpx.HTTPError, OSError) as exc:
        conn.execute(
            "INSERT INTO dai_probes (episode_id, platform, tested_at, bytes_compared, diverged)"
            " VALUES (?, ?, ?, 0, 0)",
            (episode["id"], platform, utcnow()),
        )
        conn.commit()
        return ProbeResult(episode["id"], episode["title"] or "", platform, error=str(exc))

    conn.execute(
        """
        INSERT INTO dai_probes
            (episode_id, platform, tested_at, bytes_compared, diverged,
             divergence_byte, reconverged, reconvergence_byte)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            episode["id"],
            platform,
            utcnow(),
            result.bytes_compared,
            int(result.diverged),
            result.divergence_byte,
            int(result.reconverged),
            result.reconvergence_byte,
        ),
    )
    conn.commit()
    return ProbeResult(episode["id"], episode["title"] or "", platform, result=result)


def platform_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per platform: how many episodes tested, how many diverged, how
    many of those also found a reconvergence point."""
    return conn.execute(
        """
        SELECT platform,
               COUNT(*) AS tested,
               SUM(diverged) AS diverged,
               SUM(CASE WHEN diverged = 1 AND reconverged = 1 THEN 1 ELSE 0 END) AS reconverged
        FROM dai_probes
        GROUP BY platform
        ORDER BY diverged DESC, tested DESC
        """
    ).fetchall()
