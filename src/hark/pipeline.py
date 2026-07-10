"""Extraction pipeline: run a TopicExtractor over episodes not yet extracted.

Idempotent like ingest: episodes are marked with extracted_at once processed
(even when zero topics came back, so trailers aren't re-billed every run),
and re-runs only touch unmarked episodes. Topics are deduped by Wikidata QID
first, exact label second, so "BTK" and "Dennis Rader" end up as one row.
Commits happen per episode; an interrupted run keeps its progress.

load_extractions() is the offline twin of extract_pending(): it takes
pre-computed extraction records (from a Batch API run, or a Claude session
acting as the extractor) and pushes them through the same canonicalize +
store path.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from .db import utcnow
from .extract import GENRES, ExtractedTopic, TopicExtractor
from .wikidata import WikidataMatch

Canonicalize = Callable[[str], WikidataMatch | None]


@dataclass
class ExtractResult:
    episode_id: int
    show: str
    title: str
    labels: list[str] = field(default_factory=list)
    error: str | None = None


def pending_episodes(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    sql = """
        SELECT e.id, e.title, e.description, COALESCE(s.title, s.query) AS show
        FROM episodes e JOIN shows s ON s.id = e.show_id
        WHERE e.extracted_at IS NULL
        ORDER BY e.show_id, e.pubdate
    """
    if limit is not None:
        sql += " LIMIT ?"
        return conn.execute(sql, (limit,)).fetchall()
    return conn.execute(sql).fetchall()


def upsert_topic(
    conn: sqlite3.Connection, label: str, wikidata_id: str | None, genres: tuple[str, ...]
) -> int:
    row = None
    if wikidata_id:
        row = conn.execute(
            "SELECT id FROM topics WHERE wikidata_id = ?", (wikidata_id,)
        ).fetchone()
    if row is None:
        row = conn.execute("SELECT id, wikidata_id FROM topics WHERE label = ?", (label,)).fetchone()
        if row is not None and wikidata_id and row["wikidata_id"] is None:
            conn.execute("UPDATE topics SET wikidata_id = ? WHERE id = ?", (wikidata_id, row["id"]))
    if row is None:
        cur = conn.execute(
            "INSERT INTO topics (label, wikidata_id) VALUES (?, ?)", (label, wikidata_id)
        )
        topic_id = cur.lastrowid
    else:
        topic_id = row["id"]
    for genre in genres:
        conn.execute(
            "INSERT OR IGNORE INTO topic_genres (topic_id, genre) VALUES (?, ?)",
            (topic_id, genre),
        )
    return topic_id


def _store(
    conn: sqlite3.Connection,
    episode_id: int,
    topics: list[ExtractedTopic],
    canonicalize: Canonicalize,
    source: str,
) -> list[str]:
    labels: list[str] = []
    seen: set[int] = set()
    for topic in topics:
        match = canonicalize(topic.label)
        label = match.label if match else topic.label
        qid = match.qid if match else None
        topic_id = upsert_topic(conn, label, qid, topic.genres)
        if topic_id in seen:
            continue
        seen.add(topic_id)
        conn.execute(
            """
            INSERT OR IGNORE INTO episode_topics (episode_id, topic_id, confidence, source)
            VALUES (?, ?, ?, ?)
            """,
            (episode_id, topic_id, topic.confidence, source),
        )
        labels.append(label)
    conn.execute(
        "UPDATE episodes SET extracted_at = ? WHERE id = ?", (utcnow(), episode_id)
    )
    return labels


def extract_pending(
    conn: sqlite3.Connection,
    extractor: TopicExtractor,
    canonicalize: Canonicalize,
    source: str,
    limit: int | None = None,
    on_result: Callable[[ExtractResult], None] | None = None,
    max_consecutive_errors: int = 5,
) -> list[ExtractResult]:
    """Extract topics for pending episodes; stops early on repeated failures.

    Failed episodes keep extracted_at NULL and are retried on the next run.
    """
    results: list[ExtractResult] = []
    consecutive_errors = 0
    for row in pending_episodes(conn, limit):
        result = ExtractResult(episode_id=row["id"], show=row["show"], title=row["title"] or "")
        try:
            topics = extractor.extract(row["title"] or "", row["description"])
            result.labels = _store(conn, row["id"], topics, canonicalize, source)
            conn.commit()
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


@dataclass
class CanonResult:
    old_label: str
    new_label: str
    qid: str
    merged: bool  # True when the topic was folded into an existing one


def recanonicalize(
    conn: sqlite3.Connection, canonicalize: Canonicalize
) -> list[CanonResult]:
    """Retry Wikidata canonicalization for topics without a QID.

    Used after throttled or offline runs. A fresh match either upgrades the
    topic in place (label + QID) or, when another topic already owns that QID
    or label, merges this topic into it: episode links and genres move over,
    the duplicate row is deleted.
    """
    results: list[CanonResult] = []
    rows = conn.execute("SELECT id, label FROM topics WHERE wikidata_id IS NULL").fetchall()
    for row in rows:
        match = canonicalize(row["label"])
        if match is None:
            continue
        target = conn.execute(
            "SELECT id FROM topics WHERE (wikidata_id = ? OR label = ?) AND id != ?",
            (match.qid, match.label, row["id"]),
        ).fetchone()
        if target is None:
            conn.execute(
                "UPDATE topics SET label = ?, wikidata_id = ? WHERE id = ?",
                (match.label, match.qid, row["id"]),
            )
            merged = False
        else:
            conn.execute(
                "UPDATE OR IGNORE topics SET wikidata_id = ? WHERE id = ?",
                (match.qid, target["id"]),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO episode_topics
                    (episode_id, topic_id, confidence, source)
                SELECT episode_id, ?, confidence, source FROM episode_topics
                WHERE topic_id = ?
                """,
                (target["id"], row["id"]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO topic_genres (topic_id, genre)"
                " SELECT ?, genre FROM topic_genres WHERE topic_id = ?",
                (target["id"], row["id"]),
            )
            conn.execute("DELETE FROM topics WHERE id = ?", (row["id"],))
            merged = True
        conn.commit()
        results.append(CanonResult(row["label"], match.label, match.qid, merged))
    return results


def load_extractions(
    conn: sqlite3.Connection,
    records: list[dict],
    canonicalize: Canonicalize,
    source: str,
    on_result: Callable[[ExtractResult], None] | None = None,
) -> list[ExtractResult]:
    """Store pre-computed extractions: [{episode_id, topics: [{label, genres,
    confidence}]}]. Applies the same validation as ClaudeExtractor (unknown
    genres dropped, confidence clamped, blank labels skipped). Episodes already
    extracted or unknown are reported as errors and left untouched.
    """
    results: list[ExtractResult] = []
    for record in records:
        episode_id = record.get("episode_id")
        row = conn.execute(
            """
            SELECT e.id, e.title, e.extracted_at, COALESCE(s.title, s.query) AS show
            FROM episodes e JOIN shows s ON s.id = e.show_id
            WHERE e.id = ?
            """,
            (episode_id,),
        ).fetchone()
        if row is None:
            result = ExtractResult(episode_id=episode_id or -1, show="?", title="?",
                                   error=f"unknown episode_id {episode_id!r}")
        elif row["extracted_at"] is not None:
            result = ExtractResult(episode_id=row["id"], show=row["show"],
                                   title=row["title"] or "", error="already extracted")
        else:
            topics = [
                ExtractedTopic(
                    label=str(t.get("label", "")).strip(),
                    genres=tuple(g for g in t.get("genres", ()) if g in GENRES),
                    confidence=None if t.get("confidence") is None
                    else max(0.0, min(1.0, float(t["confidence"]))),
                )
                for t in record.get("topics", [])
                if str(t.get("label", "")).strip()
            ]
            result = ExtractResult(episode_id=row["id"], show=row["show"],
                                   title=row["title"] or "")
            result.labels = _store(conn, row["id"], topics, canonicalize, source)
            conn.commit()
        results.append(result)
        if on_result:
            on_result(result)
    return results
