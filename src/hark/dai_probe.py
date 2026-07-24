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


DEFAULT_MIN_TRIALS = 3


# After this many probes on a platform with ZERO divergence, treat it as non-DAI and stop
# spending probe budget on its new episodes — that budget goes to platforms that actually
# diverge (5b). Reversible: it's derived live from dai_probes, so clearing those rows re-opens
# the platform. Well above DEFAULT_MIN_TRIALS so a platform gets a real chance to show DAI first.
PROVEN_NON_DAI_TRIALS = 40


def _proven_non_dai(conn: sqlite3.Connection, threshold: int = PROVEN_NON_DAI_TRIALS) -> set[str]:
    return {
        r["platform"] for r in conn.execute(
            "SELECT platform FROM dai_probes GROUP BY platform "
            "HAVING COUNT(*) >= ? AND COALESCE(SUM(diverged), 0) = 0", (threshold,))
    }


def select_sample(
    conn: sqlite3.Connection,
    per_platform: int = 1,
    limit: int | None = None,
    min_trials: int = DEFAULT_MIN_TRIALS,
    skip_proven_non_dai: bool = True,
) -> list[sqlite3.Row]:
    """Pick up to `per_platform` episodes needing another probe from each
    distinct hosting_platform, prioritizing whichever has the fewest attempts
    so far within a platform.

    A single probe is not a reliable verdict: acast.com was observed to flip
    from diverged to byte-identical on an otherwise-identical re-test of the
    same episode, minutes apart — some platforms' targeting has a
    randomized/inventory-dependent component this technique can't control
    for. An episode keeps being selected across separate `dai-probe` runs
    until it has `min_trials` recorded attempts, not just one — run this
    command periodically (a scheduled job, not a single one-off) to actually
    accumulate that many. platform_summary() reports diverged/tested as raw
    counts specifically so this partial-agreement is visible rather than
    collapsed into a single yes/no per platform.

    Shows with no hosting_platform yet are skipped — run
    hosting.backfill_hosting_platform() first."""
    rows = conn.execute(
        """
        SELECT e.*, s.hosting_platform,
               (SELECT COUNT(*) FROM dai_probes p WHERE p.episode_id = e.id) AS probe_count
        FROM episodes e
        JOIN shows s ON s.id = e.show_id
        WHERE e.audio_url IS NOT NULL AND s.hosting_platform IS NOT NULL
          AND (SELECT COUNT(*) FROM dai_probes p WHERE p.episode_id = e.id) < ?
        ORDER BY probe_count ASC, e.id ASC
        """,
        (min_trials,),
    ).fetchall()
    skip = _proven_non_dai(conn) if skip_proven_non_dai else set()
    per_platform_count: dict[str, int] = {}
    sample = []
    for row in rows:
        platform = row["hosting_platform"]
        if platform in skip:                       # proven non-DAI — don't waste probes here (5b)
            continue
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
