# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-07-11

### Added

- Ad-stripping via `flan/adscrub`, added as a **library dependency**
  (`[tool.uv.sources]` path dependency, editable) — not a code merge. hark's
  `episodes`/`shows`/`ad_segments` schema was deliberately shaped to match
  adscrub's own, so adscrub's schema-coupled functions work unchanged against
  hark's database: `hark chapters`/`transcribe`/`detect-ads`/`cut` call
  straight into the `adscrub` package. No duplicated pipeline code exists in
  this repo.
- `hark web` now also serves `GET /feed/<show_id>/<token>` (regenerated clean
  RSS, via hark's own `podcast_feed.py`) and `GET /audio/<episode_id>/<token>.<ext>`
  (locally-cut episodes) — unauthenticated (no cookie login, since a podcast
  app can't do that) but gated by a per-show random token. `--base-url`/
  `$HARK_BASE_URL` controls what's embedded in generated links; warns if left
  at the unreachable `localhost` default.
- `compose.gpu.yaml`: requests the host's GPU via the `nvidia` Docker runtime;
  hark's own `gpu` extra passes through to `adscrub[gpu]`.

### Changed

- Dependency, not a merge — corrected mid-session (see below) after the wrong
  approach was initially built and pushed, then reverted.

### Fixed

- An earlier pass in this same session **fully merged** adscrub's source files
  into `src/hark/` and pushed it to `main` — the wrong architecture (the
  intent was always two separate products, with hark depending on adscrub as
  a library). Reverted via `git revert -m 1` (history-preserving, not a
  force-push/reset) once caught, then rebuilt correctly as a dependency. Also:
  this merge/revert/rebuild happened concurrently with another Claude session
  actively working in this same `~/hark` checkout (uncommitted `claims.py`
  work) — branch switches were done carefully to avoid disturbing it. See
  memory `feedback_shared_working_dir` for the general lesson.

## [0.3.7] - 2026-07-10

### Fixed

- Search's episode-title-match table was missing its header row entirely,
  and showed nothing at all (no message, empty table) when a search had
  zero matches — same for the topic results, which incorrectly reused the
  empty-database message ("Nothing here yet.") for a no-results search.
  Both now say clearly that nothing matched the query.
- Topic genre lists ("history,mystery,true_crime") had no space after
  commas anywhere they're rendered as plain text.

### Added

- Home page's top-topics widget links to the full paginated `/topics` list
  when there are more topics than the widget shows.
- Small `title` tooltips on the episode play icon and the topic-detail
  confidence column header, for anyone unsure what either means.

## [0.3.6] - 2026-07-10

### Fixed

- The CSP has no `style-src 'unsafe-inline'`, so every inline `style="..."`
  attribute in the app was being silently dropped by the browser rather than
  erroring — invisible without actually rendering a page. Found by measuring
  computed layout, not by reading the HTML. Replaced all three occurrences
  (login heading spacing, account page layout) with proper CSS classes, and
  added a regression test asserting no rendered page ever contains a
  `style="` attribute.
- Account page: the password-change box and "Log out" button had an
  unintended ~130px gap between them (a direct symptom of the above — the
  margin override meant to close it was silently inert). "Log out" is now
  also visually secondary (outlined) instead of matching "Change password"'s
  primary button styling, reflecting that they're very different-stakes
  actions.
- `/shows`: rows for a show with an indexing backlog (indexed < episodes)
  now highlight the indexed count instead of looking identical to a fully
  caught-up show.

## [0.3.5] - 2026-07-10

Screenshot-driven UX audit found real usability defects, not just polish —
fixed and re-verified against the live dataset.

### Fixed

- Show pages rendered every episode on one page with no pagination — the
  411-episode Casefile True Crime page was 23,607px tall (724 episodes for
  the largest show would have been worse). Now paginated at 50/page.
- `/topics` and search results silently capped at 200 rows with no way to
  reach anything beyond that — a genre with more topics than the cap (e.g.
  history at 783) had the majority permanently unreachable through
  browsing. Now paginated, with an honest total count.
- Search's episode-title matches (capped at 50, uncapped in the underlying
  data) now say so explicitly instead of silently truncating.
- Home page's "recently indexed" show names are now links, matching every
  other place in the app where a show name appears.

## [0.3.4] - 2026-07-10

### Added

- Show detail page (`/show/<id>`): every episode for a show with its
  extracted topics linked inline, closing the loop with topic pages (which
  already linked shows -> now shows link back). Show names on `/shows` and
  the "covered by" line on topic pages both link here.
- Home page: genre breakdown with per-genre topic counts, and a "recently
  indexed" feed of the last 8 episodes processed — useful for watching a
  backfill run live, and for browsing entry points beyond the top-topics list.

## [0.3.3] - 2026-07-10

### Added

- Home page now shows an indexing-status banner: whether extraction is
  actively running, how many episodes are still queued, when the last one
  was processed, and how many topics are still awaiting Wikidata
  canonicalization — so a background load run is visible from the UI
  itself instead of only inferrable from the raw counts.

## [0.3.2] - 2026-07-10

### Changed

- Docker: the container's unprivileged user now runs as uid/gid 568 (TrueNAS
  SCALE's standard "apps" account) instead of an arbitrary 8710, so files in
  the data volume land owned consistently with every other app on that host.
  Ownership is still fixed up automatically by the entrypoint on start,
  regardless of the mounted directory's prior owner.

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
