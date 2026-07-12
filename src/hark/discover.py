"""M2: candidate-show discovery — cheap iTunes Search signals to surface
shows worth adding, before spending any real budget (extraction, GPU
transcription) on them. "Deeper analysis" (PLAN.md's phrase) isn't a second
automated stage here — it's the owner reviewing the reported candidate list,
then the existing ingest/extract pipeline once one is actually added, same
as any other show. Reuses resolve.search_podcasts() (iTunes Search, keyless)
rather than a second podcast-search API surface.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .resolve import search_podcasts

# Maps hark's own episode-subject genre vocabulary (extract.GENRES) to
# search terms that tend to surface shows iTunes files under a matching
# *show*-level category — not a 1:1 mapping (hark's genres classify what an
# episode is about; iTunes classifies the show as a whole), best-effort.
SEED_TERMS: dict[str, tuple[str, ...]] = {
    "true_crime": ("true crime", "unsolved murder"),
    "history": ("history podcast", "world history"),
    "disaster": ("disaster history",),
    "scam_fraud": ("scam podcast", "con artist"),
    "biography": ("biography podcast",),
    "espionage": ("espionage history", "spy history"),
    "cult": ("cult documentary",),
    "mystery": ("unsolved mystery",),
}


@dataclass
class Candidate:
    title: str
    feed_url: str
    genre: str | None
    author: str | None
    episode_count: int | None
    matched_term: str


def search_candidates(
    client: httpx.Client, terms: list[str] | None = None, limit_per_term: int = 10
) -> list[Candidate]:
    """Search each term, deduplicating by feed_url across terms (a show can
    plausibly match more than one seed term)."""
    if terms is None:
        terms = sorted({t for ts in SEED_TERMS.values() for t in ts})
    seen: dict[str, Candidate] = {}
    for term in terms:
        for result in search_podcasts(client, term, limit=limit_per_term):
            feed_url = result.get("feedUrl")
            if not feed_url or feed_url in seen:
                continue
            seen[feed_url] = Candidate(
                title=result.get("collectionName") or feed_url,
                feed_url=feed_url,
                genre=result.get("primaryGenreName"),
                author=result.get("artistName"),
                episode_count=result.get("trackCount"),
                matched_term=term,
            )
    return sorted(seen.values(), key=lambda c: c.episode_count or 0, reverse=True)


def filter_known(conn, candidates: list[Candidate]) -> list[Candidate]:
    """Drop any candidate whose feed_url is already a show hark tracks."""
    existing = {
        row["feed_url"]
        for row in conn.execute("SELECT feed_url FROM shows WHERE feed_url IS NOT NULL")
    }
    return [c for c in candidates if c.feed_url not in existing]
