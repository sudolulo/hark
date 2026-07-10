# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-10

M1: topic extraction + index.

### Added

- LLM topic extraction: `ClaudeExtractor` names each episode's real-world
  subject(s) from title/description using Claude structured outputs. Model
  configurable via `--model` / `$HARK_MODEL` (default `claude-opus-4-8`);
  reads `$ANTHROPIC_API_KEY`.
- Wikidata canonicalization (keyless `wbsearchentities`): aliases merge into
  one topic ("BTK" = "Dennis Rader"), topics store QIDs, unmatched labels are
  kept as-is.
- Extraction pipeline: idempotent over `episodes.extracted_at` (zero-topic
  episodes are marked too, so trailers aren't re-billed), per-episode commits,
  aborts after 5 consecutive API failures; failed episodes retry next run.
- CLI: `hark extract [--limit N] [--dry-run]`, `hark topics` (cross-show
  coverage ranking), `hark who <label-or-QID>` — the core "who covered X" query.
- Schema migration: existing 0.1.0 databases gain `episodes.extracted_at`
  automatically on connect.

### Changed

- New dependency: `anthropic` (with pydantic for schema-validated extraction).

## [0.1.0] - 2026-07-10

M0: scaffold + ingest.

### Added

- Project scaffold: uv/pyproject, src layout, pytest.
- SQLite schema: shows, episodes, topics, topic_genres, episode_topics
  (extraction fields stay NULL until M1).
- Feed resolution: show names in `feeds.txt` resolved to feed URLs via the
  keyless iTunes Search API.
- RSS ingest: fetch and parse feeds, idempotent upsert of shows and episodes
  (guid, title, description, pubdate, duration, audio URL).
- Topic extraction stub interface (`TopicExtractor`); real extraction is M1.
- CLI: `hark resolve`, `hark ingest`, `hark stats`.
- Unit tests with local feed fixtures (no network in tests).
