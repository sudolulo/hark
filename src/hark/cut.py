"""Cut ad_segments out of the downloaded audio with ffmpeg.

Ad spans can come from more than one source for the same episode (a chapter
marker and an LLM-flagged span might both cover roughly the same ad break, or
overlap partially). Rather than pick a "winning" source, merge overlapping
spans at cut time — no dedup rule needed at the schema level.

Approach: ffmpeg -ss/-to stream-copy extraction of each surviving (non-ad)
span, then the concat demuxer glues them back together — no re-encoding, so
no quality loss and no cost proportional to episode length.

Ported from the standalone adscrub repo (see docs/PLAN.md).
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .audio import DEFAULT_DATA_DIR, download_audio, probe_duration
from .db import utcnow


def compute_keep_spans(
    ad_spans: list[tuple[float, float]], duration: float
) -> list[tuple[float, float]]:
    """Merge overlapping/adjacent ad spans, then return the complementary spans to keep."""
    if not ad_spans:
        return [(0.0, duration)]
    merged: list[list[float]] = []
    for start, end in sorted(ad_spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    keep = []
    cursor = 0.0
    for start, end in merged:
        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))
        if start > cursor:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        keep.append((cursor, duration))
    return keep


def cut_audio(audio_path: Path, keep_spans: list[tuple[float, float]], output_path: Path) -> None:
    """Write the audio restricted to keep_spans to output_path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if len(keep_spans) == 1 and keep_spans[0][0] == 0.0:
        shutil.copyfile(audio_path, output_path)  # nothing to cut
        return
    with tempfile.TemporaryDirectory() as tmp:
        segment_paths = []
        for i, (start, end) in enumerate(keep_spans):
            seg_path = Path(tmp) / f"seg_{i}{audio_path.suffix}"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(audio_path), "-ss", str(start), "-to", str(end),
                 "-c", "copy", str(seg_path)],
                capture_output=True, check=True,
            )
            segment_paths.append(seg_path)
        concat_list = Path(tmp) / "concat.txt"
        concat_list.write_text("".join(f"file '{p}'\n" for p in segment_paths))
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c", "copy", str(output_path)],
            capture_output=True, check=True,
        )


@dataclass
class CutResult:
    episode_id: int
    title: str
    ad_seconds: float = 0.0
    error: str | None = None


def pending_episodes(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Episodes with at least one ad span found, not yet cut."""
    query = """
        SELECT * FROM episodes
        WHERE cut_path IS NULL
          AND EXISTS (SELECT 1 FROM ad_segments WHERE episode_id = episodes.id)
        ORDER BY id
    """
    if limit:
        query += " LIMIT ?"
        return conn.execute(query, (limit,)).fetchall()
    return conn.execute(query).fetchall()


def cut_episode(
    conn: sqlite3.Connection,
    episode: sqlite3.Row,
    client: httpx.Client,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> tuple[Path, float]:
    """Download (if needed), cut ad spans out, update the episode row.

    Returns (cut_path, ad_seconds_removed).
    """
    audio_path = download_audio(
        client, episode["audio_url"], data_dir / "audio" / f"{episode['id']}.mp3"
    )
    duration = probe_duration(audio_path)
    ad_spans = [
        (row["start_second"], row["end_second"])
        for row in conn.execute(
            "SELECT start_second, end_second FROM ad_segments WHERE episode_id = ?",
            (episode["id"],),
        )
    ]
    keep_spans = compute_keep_spans(ad_spans, duration)
    ad_seconds = duration - sum(end - start for start, end in keep_spans)

    output_path = data_dir / "cut" / f"{episode['id']}{audio_path.suffix}"
    cut_audio(audio_path, keep_spans, output_path)

    conn.execute(
        "UPDATE episodes SET cut_path = ?, updated_at = ? WHERE id = ?",
        (str(output_path), utcnow(), episode["id"]),
    )
    conn.commit()
    return output_path, ad_seconds


def cut_pending(
    conn: sqlite3.Connection,
    client: httpx.Client,
    data_dir: Path = DEFAULT_DATA_DIR,
    limit: int | None = None,
    on_result: Callable[[CutResult], None] | None = None,
) -> list[CutResult]:
    results: list[CutResult] = []
    for row in pending_episodes(conn, limit):
        result = CutResult(episode_id=row["id"], title=row["title"] or "")
        try:
            _path, ad_seconds = cut_episode(conn, row, client, data_dir)
            result.ad_seconds = ad_seconds
        except Exception as exc:  # noqa: BLE001 — per-episode isolation
            result.error = str(exc)
        results.append(result)
        if on_result:
            on_result(result)
    return results
