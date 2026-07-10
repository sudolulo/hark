# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.1] - 2026-07-10

Hardening pass ahead of the TrueNAS deploy: a full audit surfaced 15 issues,
all fixed and covered by regression tests.

### Fixed

- `hark canon`: a topic sharing another entity's Wikidata display label
  (e.g. "Mercury" the planet vs. the element) could silently overwrite that
  entity's QID and merge unrelated episodes onto it. Only an actual QID match
  (or an unresolved same-label topic) is now treated as a merge target; a
  genuine label collision between two resolved entities is disambiguated
  with the QID instead of colliding on the `topics.label` unique constraint.
- Web UI: a missing or not-yet-created `hark.db` (e.g. a fresh volume before
  the first ingest) crashed every authenticated route with an unhandled
  exception and no HTTP response; now returns a clear 503.
- Web UI: POST requests that redirected before reading the body (expired
  session, unmatched route) left it undrained, desyncing the next
  HTTP/1.1 keep-alive request on the same connection; the body is now always
  consumed. An oversized body now closes the connection instead of risking
  the same desync.
- Docker: the non-root `hark` user couldn't write a freshly-created bind
  mount or volume (Docker creates these as root), so the container
  crash-looped on first start. The entrypoint now fixes ownership as root
  before dropping to the unprivileged user via `gosu`.
- `hark load`: a malformed record no longer aborts the whole batch — each
  record is isolated like `hark extract` already isolates episodes.
  Re-loading already-extracted episodes is now reported as a skip, not a
  failure (previously exit code 1 on an idempotent re-run).
- Wikidata canonicalizer: a `Retry-After` header in HTTP-date form (RFC 7231
  permits either format) crashed and was silently swallowed as "no match";
  parsing now handles both forms and caps the wait. Transport-level failures
  (timeouts, connection resets) now retry like throttling responses do,
  instead of giving up on the first attempt.

### Changed

- Consolidated three near-duplicate topic-listing queries (home, `/topics`,
  `/search`, and `hark topics`) into one query builder; `/search`'s topic
  results are now capped like every other list view.
- `GENRES_FILTER` in the web UI is no longer a second copy of `extract.GENRES`.

## [0.3.0] - 2026-07-10

Web frontend + deployment.

### Added

- Web UI (`hark web`, dependency-free stdlib server): index dashboard, topic
  list with genre filters, topic pages ("who covered X" across shows, with
  episode dates, confidence and audio links), search over topic labels and
  episode titles, show list with indexing progress.
- Security model per the influence-registry spec: whole site behind a session
  login wall (only `/login`, `/logout`, `/healthz`, `/static/*` open);
  fail-closed when no password and no `HARK_ADMIN_TOKEN` exist; server-side
  sessions in HttpOnly/SameSite=Lax cookies (`HARK_COOKIE_SECURE=1` adds
  `Secure` behind a TLS proxy); iterated salted SHA-256 password stretching
  with constant-time compare; password change revokes all sessions; strict
  security headers (CSP `default-src 'self'`, nosniff, DENY framing,
  same-origin referrer) on every response.
- Auth state lives in a separate `auth.db` so data snapshots can replace
  `hark.db` without wiping accounts or sessions.
- `hark load` — ingest pre-computed extraction JSONL (batch runs or session
  output) through the same canonicalize + store path.
- `hark canon` — retry Wikidata canonicalization for unmatched topics,
  merging duplicates that resolve to an existing QID.
- Canonicalizer: politeness delay between lookups and retry with backoff on
  429/5xx (previously throttled responses were swallowed as "no match").
- Dockerfile + compose (tiltmeter pattern): web UI by default, every pipeline
  stage as a one-shot command; data volume at `/app/data`.

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
