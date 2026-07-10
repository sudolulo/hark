# hark — plan

Milestones. Each one ships something usable and gets a CHANGELOG version.

## M0 — scaffold + ingest (current)

- Project scaffold: uv/pyproject, src layout, pytest.
- SQLite schema: shows, episodes, topics, episode_topics (extraction fields nullable —
  populated in M1).
- Feed resolution: show names in `feeds.txt` → feed URLs via iTunes Search API (keyless).
- RSS ingest: fetch + parse feeds, upsert shows/episodes (id, title, description, pubdate,
  duration, audio URL). Idempotent re-runs.
- CLI: `hark resolve`, `hark ingest`, `hark stats`.
- Unit tests with feed fixtures (no network in tests).

## M1 — topic extraction + index

- LLM extraction of subject entities from title/description (stub interface in M0; model
  wiring decided when we get here).
- Canonicalization against Wikidata (aliases: "BTK" = "Dennis Rader"); multi-part/serial
  episode handling; multi-genre topics.
- Topic pages: "who covered X" — the core query.

## M2 — discovery

- Embedding similarity over episode topics → related shows, notable back-catalog episodes.
- Candidate-show pipeline: cheap signals first, deeper analysis only for shows that pass.

## M3 — AntennaPod loop

- Read subscriptions/history from Nextcloud gpodder sync (truenas).
- Generate custom RSS feeds as the recommendation delivery channel.

## M4 — episode scoring (tiltmeter-style)

- Defined interestingness metrics, calibration loop against owner ratings.
- Per-topic treatment comparison (depth, sensationalism) — needs transcripts for fidelity;
  revisit Whisper here, not before.

## Seed shows (feeds.txt)

Start with well-known subject-per-episode shows across two genres, e.g.: Casefile,
Casual Criminalist, Swindled, The Rest Is History, Short History Of, Cautionary Tales.
Resolve their real feed URLs via iTunes Search API at runtime — do not hand-copy URLs.

## Open questions (owner input needed, don't block on these)

- ~~Hosting~~ Resolved 2026-07-10: private Gitea (`flan/hark`); revisit GitHub if it goes public.
- Which LLM/provider for extraction (M1 decision).
- GPU/Whisper feasibility on this LXC (M4 decision; CUDA device nodes may not be exposed).
