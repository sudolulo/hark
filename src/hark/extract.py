"""Topic extraction: LLM extraction of episode subjects (M1).

ClaudeExtractor asks a Claude model for the real-world subject(s) of an
episode based on its title/description, using structured outputs so the
response is schema-validated JSON. Labels are canonicalized against
Wikidata afterwards (see wikidata.py) — the model only has to name the
subject, not pick the canonical form.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

DEFAULT_MODEL = "claude-opus-4-8"

# Fixed genre vocabulary; a topic may carry several (Titanic = history + disaster).
GENRES = (
    "true_crime",
    "history",
    "disaster",
    "scam_fraud",
    "biography",
    "espionage",
    "cult",
    "mystery",
    "other",
)

_SYSTEM = """\
You index subject-per-episode podcasts (true crime, history, disasters, scams,
espionage, cults, mysteries). Given one episode's title and description, name the
real-world subject(s) the episode covers: the case, event, person, place, or
phenomenon — e.g. "Dyatlov Pass incident", "Dennis Rader", "Sinking of the Titanic".

Rules:
- Use the most recognizable English name for each subject, as a Wikipedia article
  would title it. Expand aliases: "BTK" is "Dennis Rader".
- Multi-part episodes ("Part 2", "Pt. II") still name the same subject.
- Most episodes have exactly one subject; a few genuinely cover two or three.
- Return an empty list for episodes with no real-world subject: trailers,
  announcements, Q&A/mailbag episodes, host chat, compilations, ads.
- genres: which of {genres} apply to the subject itself (not the show). Multiple
  genres are fine.
- confidence: 0.0-1.0 that this subject is what the episode is about.
""".format(genres=", ".join(GENRES))


@dataclass
class ExtractedTopic:
    label: str
    genres: tuple[str, ...] = ()
    confidence: float | None = None
    wikidata_id: str | None = None


class TopicExtractor(Protocol):
    def extract(self, title: str, description: str | None) -> list[ExtractedTopic]: ...


class NullExtractor:
    """Placeholder that extracts nothing (used by tests and dry paths)."""

    def extract(self, title: str, description: str | None) -> list[ExtractedTopic]:
        return []


class _Topic(BaseModel):
    label: str
    genres: list[str]
    confidence: float


class _Extraction(BaseModel):
    topics: list[_Topic]


class ClaudeExtractor:
    """Extract episode subjects with a Claude model via structured outputs.

    `client` is an anthropic.Anthropic instance (or any object with a
    compatible messages.parse) — injected so tests never touch the network.
    """

    def __init__(self, client, model: str = DEFAULT_MODEL):
        self.client = client
        self.model = model

    def extract(self, title: str, description: str | None) -> list[ExtractedTopic]:
        body = f"Title: {title}\n\nDescription: {description or '(none)'}"
        # Descriptions occasionally carry whole-show boilerplate; cap the size
        # so one bloated feed doesn't dominate the token bill.
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": body[:4000]}],
            output_format=_Extraction,
        )
        parsed = response.parsed_output
        if parsed is None:  # refusal or malformed output
            return []
        return [
            ExtractedTopic(
                label=t.label.strip(),
                genres=tuple(g for g in t.genres if g in GENRES),
                confidence=max(0.0, min(1.0, t.confidence)),
            )
            for t in parsed.topics
            if t.label.strip()
        ]
