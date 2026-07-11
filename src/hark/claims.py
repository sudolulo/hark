"""Cross-show claims comparison for topics covered by 2+ shows.

hark's core question is "who covered X" — this module answers the natural
follow-up: what does each show actually *say* about X, and where do their
tellings agree or diverge? A literal text diff between independently
scripted episodes is close to useless (different hosts, different wording,
same underlying facts), so instead of diffing raw transcripts — or
extracting claims per episode and then trying to fuzzy-match them against
each other, which just moves the same matching problem one step later —
this asks the model directly for a structured comparison across all of a
topic's transcribed episodes in one call: which claims are shared, and
which are unique to one show's telling.

Follows the same structured-outputs idiom as extract.py's ClaudeExtractor
and detect.py's ClaudeAdDetector.

Owns its own SQLite table (topic_comparisons) rather than joining db.py's
shared schema — self-contained via ensure_schema() below, same idiom as
web.py's Auth does for auth.db. Wired in as: `hark compare` /
`hark load-comparisons` in cli.py, and the /episode/<id> page in web.py
(get_comparison(), read-only-connection-safe — see its own docstring).

Depends on transcripts existing at episodes.transcript_path (produced by
transcribe.py, itself ported from adscrub) — this module only reads that
column, never writes it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from pydantic import BaseModel

DEFAULT_MODEL = os.environ.get("HARK_CLAIMS_MODEL", "claude-opus-4-8")

# Per-episode transcript cap fed into one comparison call — a topic with 3+
# hour-long episodes could otherwise blow well past a sane token budget.
MAX_TRANSCRIPT_CHARS = 60_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS topic_comparisons (
    topic_id       INTEGER PRIMARY KEY REFERENCES topics(id) ON DELETE CASCADE,
    episode_ids    TEXT NOT NULL,  -- JSON list[int]; compared against current
                                    -- state each run so a newly-transcribed
                                    -- episode triggers a refresh, not a skip
    shared         TEXT NOT NULL,  -- JSON list[str]
    unique_by_show TEXT NOT NULL,  -- JSON {show_name: list[str]}
    model          TEXT NOT NULL,
    generated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


_SYSTEM = """\
You compare how different podcasts covered the same real-world case, event,
or person. You will be given transcripts of two or more episodes, each from
a different show, about the same subject. Identify:

- shared: factual claims made by two or more of the episodes — the same
  underlying fact even if worded differently (dates, names, sequence of
  events, causes, outcomes).
- unique_by_show: factual claims made by only one episode, that the others
  omit or contradict — keyed by that episode's show name exactly as given.

Focus on substantive claims (what happened, who was involved, when, why,
how it was resolved), not style, tone, or presentation choices. A handful
of well-chosen claims is more useful than an exhaustive list — aim for the
5-15 most load-bearing facts, not everything mentioned.
"""


@dataclass
class Comparison:
    shared: list[str] = field(default_factory=list)
    unique_by_show: dict[str, list[str]] = field(default_factory=dict)


class Comparator(Protocol):
    def compare(self, episodes: dict[str, str]) -> Comparison:
        """`episodes` maps show name -> transcript text (2+ entries)."""
        ...


class NullComparator:
    """Placeholder that finds nothing (used by tests and dry paths)."""

    def compare(self, episodes: dict[str, str]) -> Comparison:
        return Comparison()


class _ComparisonPayload(BaseModel):
    shared: list[str]
    unique_by_show: dict[str, list[str]]


class ClaudeComparator:
    """Compare episode transcripts with a Claude model via structured outputs.

    `client` is an anthropic.Anthropic instance (or any object with a
    compatible messages.parse) — injected so tests never touch the network.
    """

    def __init__(self, client, model: str = DEFAULT_MODEL):
        self.client = client
        self.model = model

    def compare(self, episodes: dict[str, str]) -> Comparison:
        if len(episodes) < 2:
            return Comparison()
        body = "\n\n".join(
            f"=== {show} ===\n{text[:MAX_TRANSCRIPT_CHARS]}" for show, text in episodes.items()
        )
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=4096,
            system=_SYSTEM,
            messages=[{"role": "user", "content": body}],
            output_format=_ComparisonPayload,
        )
        parsed = response.parsed_output
        if parsed is None:  # refusal or malformed output
            return Comparison()
        known_shows = set(episodes)
        unique = {
            show: [c.strip() for c in claims if c.strip()]
            for show, claims in parsed.unique_by_show.items()
            if show in known_shows  # drop hallucinated show names
        }
        return Comparison(
            shared=[c.strip() for c in parsed.shared if c.strip()],
            unique_by_show={show: claims for show, claims in unique.items() if claims},
        )


def transcript_text(path: str) -> str:
    """Flatten a transcribe.py-produced JSON segment list to plain text."""
    segments = json.loads(Path(path).read_text(encoding="utf-8"))
    return " ".join(seg["text"] for seg in segments if seg.get("text"))


def pending_topics(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """Topics with transcripts from 2+ distinct shows, where no comparison
    exists yet or the transcribed episode set has changed since the last
    one (a newly-transcribed episode should trigger a refresh)."""
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT et.topic_id, e.id AS episode_id, e.transcript_path,
               COALESCE(s.title, s.query) AS show
        FROM episode_topics et
        JOIN episodes e ON e.id = et.episode_id
        JOIN shows s ON s.id = e.show_id
        WHERE e.transcript_path IS NOT NULL
        ORDER BY et.topic_id
        """
    ).fetchall()
    by_topic: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        by_topic.setdefault(r["topic_id"], []).append(r)

    existing = {
        r["topic_id"]: json.loads(r["episode_ids"])
        for r in conn.execute("SELECT topic_id, episode_ids FROM topic_comparisons")
    }

    pending = []
    for topic_id, episodes in by_topic.items():
        if len({e["show"] for e in episodes}) < 2:
            continue
        episode_ids = sorted(e["episode_id"] for e in episodes)
        if existing.get(topic_id) == episode_ids:
            continue  # already compared against this exact episode set
        pending.append({"topic_id": topic_id, "episodes": episodes})
        if limit and len(pending) >= limit:
            break
    return pending


@dataclass
class CompareResult:
    topic_id: int
    label: str = ""
    shared_count: int = 0
    error: str | None = None


def compare_pending(
    conn: sqlite3.Connection,
    comparator: Comparator,
    limit: int | None = None,
    on_result: Callable[[CompareResult], None] | None = None,
    max_consecutive_errors: int = 5,
) -> list[CompareResult]:
    """Run comparison over pending topic groups; stops early on repeated failures."""
    ensure_schema(conn)
    results: list[CompareResult] = []
    consecutive_errors = 0
    for item in pending_topics(conn, limit):
        topic_id = item["topic_id"]
        label = conn.execute(
            "SELECT label FROM topics WHERE id = ?", (topic_id,)
        ).fetchone()["label"]
        result = CompareResult(topic_id=topic_id, label=label)
        try:
            episodes = {
                e["show"]: transcript_text(e["transcript_path"]) for e in item["episodes"]
            }
            comparison = comparator.compare(episodes)
            episode_ids = sorted(e["episode_id"] for e in item["episodes"])
            conn.execute(
                """
                INSERT INTO topic_comparisons
                    (topic_id, episode_ids, shared, unique_by_show, model)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (topic_id) DO UPDATE SET
                    episode_ids = excluded.episode_ids, shared = excluded.shared,
                    unique_by_show = excluded.unique_by_show, model = excluded.model,
                    generated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                """,
                (
                    topic_id,
                    json.dumps(episode_ids),
                    json.dumps(comparison.shared),
                    json.dumps(comparison.unique_by_show),
                    getattr(comparator, "model", "unknown"),
                ),
            )
            conn.commit()
            result.shared_count = len(comparison.shared)
            consecutive_errors = 0
        except Exception as exc:  # noqa: BLE001 — per-topic isolation, abort on streaks
            conn.rollback()
            result.error = str(exc)
            consecutive_errors += 1
        results.append(result)
        if on_result:
            on_result(result)
        if consecutive_errors >= max_consecutive_errors:
            break
    return results


@dataclass
class LoadResult:
    topic_id: int
    label: str = ""
    shared_count: int = 0
    error: str | None = None


def load_comparisons(
    conn: sqlite3.Connection,
    records: list[dict],
    model: str = "session",
    on_result: Callable[[LoadResult], None] | None = None,
) -> list[LoadResult]:
    """Load pre-computed comparisons — same idiom as pipeline.load_extractions
    for topic extraction: this Claude session acts as the comparator directly
    (no ANTHROPIC_API_KEY needed) and its output is loaded here instead of
    going through ClaudeComparator. Each record:
    {"topic_id": int, "shared": [str], "unique_by_show": {show: [str]}}.
    """
    ensure_schema(conn)
    results: list[LoadResult] = []
    for rec in records:
        topic_id = rec["topic_id"]
        topic = conn.execute("SELECT label FROM topics WHERE id = ?", (topic_id,)).fetchone()
        result = LoadResult(topic_id=topic_id, label=topic["label"] if topic else "")
        if topic is None:
            result.error = "no such topic"
            results.append(result)
            if on_result:
                on_result(result)
            continue
        episode_ids = sorted(
            r["id"] for r in conn.execute(
                """
                SELECT DISTINCT e.id FROM episode_topics et
                JOIN episodes e ON e.id = et.episode_id
                WHERE et.topic_id = ? AND e.transcript_path IS NOT NULL
                """,
                (topic_id,),
            )
        )
        conn.execute(
            """
            INSERT INTO topic_comparisons
                (topic_id, episode_ids, shared, unique_by_show, model)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (topic_id) DO UPDATE SET
                episode_ids = excluded.episode_ids, shared = excluded.shared,
                unique_by_show = excluded.unique_by_show, model = excluded.model,
                generated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (
                topic_id,
                json.dumps(episode_ids),
                json.dumps(rec["shared"]),
                json.dumps(rec["unique_by_show"]),
                model,
            ),
        )
        conn.commit()
        result.shared_count = len(rec["shared"])
        results.append(result)
        if on_result:
            on_result(result)
    return results


def get_comparison(conn: sqlite3.Connection, topic_id: int) -> Comparison | None:
    """Read-only lookup — deliberately doesn't call ensure_schema(), so this
    works against a read-only connection (hark.web's App.db()) even before
    the table exists (e.g. `hark compare` has never been run yet)."""
    try:
        row = conn.execute(
            "SELECT shared, unique_by_show FROM topic_comparisons WHERE topic_id = ?", (topic_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return Comparison(shared=json.loads(row["shared"]), unique_by_show=json.loads(row["unique_by_show"]))
