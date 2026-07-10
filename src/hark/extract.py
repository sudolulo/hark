"""Topic extraction interface — stub until M1.

M1 wires an LLM behind TopicExtractor and canonicalizes labels against
Wikidata (see docs/PLAN.md). M0 ships only the interface so the schema and
pipeline shape are settled; nothing calls a model yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ExtractedTopic:
    label: str
    genres: tuple[str, ...] = ()
    confidence: float | None = None
    wikidata_id: str | None = None


class TopicExtractor(Protocol):
    def extract(self, title: str, description: str | None) -> list[ExtractedTopic]: ...


class NullExtractor:
    """M0 placeholder: extracts nothing."""

    def extract(self, title: str, description: str | None) -> list[ExtractedTopic]:
        return []
