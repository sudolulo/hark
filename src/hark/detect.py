"""Classify ad spans from a transcript via LLM call.

ClaudeAdDetector asks the model to point at *segment indices* rather than raw
timestamps — LLMs are unreliable at reproducing exact floating-point numbers
from memory but reliable at picking items from a numbered list. Indices are
mapped back to the transcript's own start/end times afterwards, so a stored
span is always grounded in what Whisper actually produced, never hallucinated.

This is the step fingerprinting/crowdsourcing can't replace: modern podcast
ads are frequently host-read and/or dynamically inserted per listener, so
there's often nothing stable to match against a known-ad database — the
model has to read the words.

Ported from the standalone adscrub repo (see docs/PLAN.md). Uses the same
structured-outputs idiom as extract.py's ClaudeExtractor, but this is a
separate model default (`HARK_AD_MODEL`, not `HARK_MODEL`) — topic extraction
and ad-span classification are different-shaped tasks that may warrant
different models.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Callable, Protocol

from pydantic import BaseModel

from .db import utcnow

DEFAULT_MODEL = os.environ.get("HARK_AD_MODEL", "claude-opus-4-8")

_SYSTEM = """\
You find ad/sponsor reads in a podcast episode transcript. The transcript is a
numbered list of timestamped segments. Identify contiguous runs of segments
that are advertising, not editorial content: sponsor mentions ("brought to you
by", "this episode is sponsored by"), promo/discount codes, URLs to sponsor
sites, or a clear tonal pitch-switch mid-episode. Do not flag ordinary
editorial content, including the show's own self-promotion of its Patreon or
merch, unless it reads as a distinct inserted sponsor segment.

For each ad span, give the index of its first and last segment (inclusive,
0-indexed) and a short reason. If there are no ads, return an empty list.
"""


@dataclass
class DetectedAdSpan:
    start_second: float
    end_second: float
    reason: str


class AdSpanDetector(Protocol):
    def detect(self, transcript: list[dict]) -> list[DetectedAdSpan]: ...


class NullDetector:
    """Placeholder that detects nothing (used by tests and dry paths)."""

    def detect(self, transcript: list[dict]) -> list[DetectedAdSpan]:
        return []


class _Span(BaseModel):
    start_segment: int
    end_segment: int
    reason: str


class _Detection(BaseModel):
    ad_spans: list[_Span]


class ClaudeAdDetector:
    """Detect ad spans with a Claude model via structured outputs.

    `client` is an anthropic.Anthropic instance (or any object with a
    compatible messages.parse) — injected so tests never touch the network.
    """

    def __init__(self, client, model: str = DEFAULT_MODEL):
        self.client = client
        self.model = model

    def detect(self, transcript: list[dict]) -> list[DetectedAdSpan]:
        if not transcript:
            return []
        body = "\n".join(
            f"[{i}] {seg['start']:.1f}-{seg['end']:.1f}: {seg['text']}"
            for i, seg in enumerate(transcript)
        )
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=2048,
            system=_SYSTEM,
            # a bloated transcript shouldn't dominate the token bill
            messages=[{"role": "user", "content": body[:20000]}],
            output_format=_Detection,
        )
        parsed = response.parsed_output
        if parsed is None:  # refusal or malformed output
            return []
        n = len(transcript)
        spans = []
        for span in parsed.ad_spans:
            if not (0 <= span.start_segment <= span.end_segment < n):
                continue
            spans.append(
                DetectedAdSpan(
                    start_second=transcript[span.start_segment]["start"],
                    end_second=transcript[span.end_segment]["end"],
                    reason=span.reason.strip(),
                )
            )
        return spans


@dataclass
class DetectResult:
    episode_id: int
    title: str
    found: int = 0
    error: str | None = None


def pending_episodes(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Episodes with a transcript that haven't been run through LLM detection yet."""
    query = """
        SELECT * FROM episodes
        WHERE transcript_path IS NOT NULL AND llm_detected_at IS NULL
        ORDER BY id
    """
    if limit:
        query += " LIMIT ?"
        return conn.execute(query, (limit,)).fetchall()
    return conn.execute(query).fetchall()


def _store(conn: sqlite3.Connection, episode_id: int, spans: list[DetectedAdSpan]) -> None:
    """Store any detected spans and mark the episode processed.

    Marks llm_detected_at even when zero spans are found — otherwise an
    episode with no ads gets re-sent to the LLM (and re-billed) every run.
    """
    for span in spans:
        conn.execute(
            """
            INSERT INTO ad_segments (episode_id, start_second, end_second, source, reason)
            VALUES (?, ?, ?, 'llm', ?)
            """,
            (episode_id, span.start_second, span.end_second, span.reason),
        )
    now = utcnow()
    conn.execute(
        "UPDATE episodes SET llm_detected_at = ?, updated_at = ? WHERE id = ?",
        (now, now, episode_id),
    )


def detect_pending(
    conn: sqlite3.Connection,
    detector: AdSpanDetector,
    limit: int | None = None,
    on_result: Callable[[DetectResult], None] | None = None,
    max_consecutive_errors: int = 5,
) -> list[DetectResult]:
    """Run detection over pending episodes; stops early on repeated failures.

    Failed episodes are left unmarked (no 'llm' ad_segments row) and are
    retried on the next run.
    """
    results: list[DetectResult] = []
    consecutive_errors = 0
    for row in pending_episodes(conn, limit):
        result = DetectResult(episode_id=row["id"], title=row["title"] or "")
        try:
            with open(row["transcript_path"], encoding="utf-8") as fh:
                transcript = json.load(fh)
            spans = detector.detect(transcript)
            _store(conn, row["id"], spans)
            conn.commit()
            result.found = len(spans)
            consecutive_errors = 0
        except Exception as exc:  # noqa: BLE001 — per-episode isolation, abort on streaks
            conn.rollback()
            result.error = str(exc)
            consecutive_errors += 1
        results.append(result)
        if on_result:
            on_result(result)
        if consecutive_errors >= max_consecutive_errors:
            break
    return results
