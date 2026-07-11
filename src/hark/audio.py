"""Shared audio-file helpers: download + duration probing.

Used by both transcribe.py (Whisper input) and cut.py (ffmpeg input) — neither
pipeline stage owns the downloaded-episode-audio cache more than the other.
Ported from the standalone adscrub repo (see docs/PLAN.md).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx

DEFAULT_DATA_DIR = Path(os.environ.get("HARK_DATA_DIR", "data"))


def download_audio(client: httpx.Client, audio_url: str, dest: Path) -> Path:
    """Fetch episode audio to dest if not already cached there."""
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with client.stream("GET", audio_url) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    tmp.rename(dest)
    return dest


def probe_duration(path: Path) -> float:
    """Audio duration in seconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())
