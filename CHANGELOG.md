# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.1] - 2026-07-12

### Fixed

- **The deployed `transcribe` container never actually transcribed anything.**
  The `hark` user is created `--no-create-home`, so huggingface_hub's default
  cache location (`~/.cache/huggingface`) resolved to an unwritable
  `/home/hark`. Every model load failed to persist its revision-check
  bookkeeping and re-hit the HF Hub API from scratch on every episode,
  which exhausted the anonymous rate limit within seconds of container
  start and kept it exhausted — 7+ hours stuck at 0 completed episodes
  despite the service reporting "running". Fixed by pointing `HF_HOME` at
  `/app/data/.hf-cache`, which is both writable by `hark` and persists
  across container restarts.
- **`hark transcribe` had no consecutive-failure circuit breaker**, unlike
  `hark detect-ads`. A rate limit or outage burned through the entire
  pending list every 5-minute cycle, re-triggering the same failure on
  every episode instead of backing off. Now aborts after 5 consecutive
  failures, matching `cmd_detect_ads`.

## [0.9.0] - 2026-07-11

Post-0.8.0 audit pass (10-angle review of everything since 0.4.0) — found and
fixed 5 real correctness bugs plus a hot-path performance gap, all with new
regression tests. See the audit findings below for detail; this entry covers
what changed, not the review methodology itself.

### Fixed

- **`compare_pending()`/`pending_topics()` (claims.py) keyed episodes by show
  *display name* instead of `show_id`.** A topic with 2+ episodes from the
  same show (a multi-part case) or two shows sharing a display name (only
  `shows.query` is UNIQUE, not `shows.title`) silently dropped one
  transcript from the LLM comparison, while `episode_ids` still recorded
  both — so the loss was permanent and untraceable, never retried. Fixed by
  grouping by `show_id` and concatenating same-show transcripts
  (`_group_transcripts_by_show()`) instead of overwriting.
- **`load_comparisons()` had no per-record error isolation**, contradicting
  its own docstring ("same idiom as `pipeline.load_extractions`") and its
  sibling `compare_pending()`. A malformed JSONL record (e.g. missing
  `"shared"`) raised uncaught and aborted the whole batch instead of being
  reported as one failed record. Now wrapped in the same per-record
  try/except/rollback pattern as `compare_pending`/`load_extractions`.
- **`cmd_detect_ads` lost the consecutive-failure circuit breaker**
  (`max_consecutive_errors=5`) that adscrub's bulk `detect_pending()` had,
  when it was rewritten to call `detect_episode()` per-episode directly so
  the per-show `ad_stripping_enabled` filter could apply. A revoked/invalid
  `ANTHROPIC_API_KEY` would previously abort after 5 failures; the
  rewritten loop just burned through the entire pending list instead.
  Restored the same abort behavior directly in cli.py.
- **`cmd_chapters`/`cmd_transcribe`/`cmd_detect_ads`/`cmd_cut`'s FAIL/ok
  print lines dropped the `title or ""` null-guard** that adscrub's
  original result construction had — a `NULL` episode title printed the
  literal string `"None"`.
- **`App.toggle_ad_stripping()` had an unguarded read-modify-write race** —
  two concurrent toggle requests could both read the same starting state
  and collapse into one net change instead of canceling out. Fixed with a
  single atomic `UPDATE shows SET ad_stripping_enabled = 1 -
  ad_stripping_enabled` instead of read-then-write in Python.
- `episode_topics` had no index on `topic_id` (only the `episode_id`-leading
  primary key), so 0.7.0/0.8.0's related-shows/related-topics features and
  `view_topic`'s episode list all full-scanned the table on every page
  view. Added `idx_episode_topics_topic`.
- `web.py`'s pluralization for search's "episode title match(es)" hand-rolled
  the exact singular/plural branch the `plural()` helper (added earlier in
  the same diff that introduced this) exists to avoid — extended `plural()`
  to accept an irregular plural form and used it here instead.

### Changed

- `CompareResult`/`LoadResult` (claims.py) were field-for-field identical
  dataclasses; consolidated into one `CompareResult` used by both
  `compare_pending()` and `load_comparisons()`.
- The `INSERT ... ON CONFLICT` write to `topic_comparisons` was duplicated
  verbatim in both `compare_pending()` and `load_comparisons()`; factored
  into a shared `_store_comparison()`.
- `view_episode`'s topic-pill rendering reimplemented `topic_pills()` inline;
  now calls it directly.
- The ad-stripped feed URL was built identically in both `web.py`'s
  `view_show()` and `podcast_feed.build_feed()`; factored into
  `podcast_feed.feed_url()`, used by both.
- `hark transcribe` gained `--cross-show-only`: restricts to episodes
  covering a topic 2+ shows have also covered — the actual priority subset
  claims comparison needs, instead of adscrub's full-corpus default scope
  (every episode with audio). Needed to run the transcription pipeline as a
  proper hark feature (via `episodes_needing_transcription()`) rather than
  an ad-hoc throwaway script.

## [0.8.0] - 2026-07-11

### Added

- Related topics on each topic page (`web.related_topics()`), ranked by how
  many episodes mention both — e.g. Fred West's page now surfaces Rosemary
  West (their cases are inseparable). Same topic-co-occurrence idiom as
  0.7.0's related shows, one level down.

### Fixed

- Topic 236's label was "Fred Wes" (missing the final "t") — not hark's own
  extraction this time, but a faithfully-mirrored typo in Wikidata's own
  entity label for Q577052 (confirmed: correct person, right dates, just a
  bad label upstream). Corrected the local label to "Fred West"; the QID
  itself was already right.

## [0.7.0] - 2026-07-11

### Added

- M2 discovery, first cut: each show page now lists related shows, ranked by
  how many topics they share (`web.related_shows()`). The original M2 spec
  called for embedding similarity; this uses the topic-coverage data already
  produced by M1 extraction instead, so it needed no new model or API key —
  171 topics already have 2+ show coverage across the full corpus. Revisit
  with real embeddings later if this co-occurrence signal proves limiting.

## [0.6.2] - 2026-07-11

### Fixed

- Proper pluralization ("1 episode" / "2 episodes") across the UI, replacing
  the placeholder "episode(s)"/"show(s)"/"topic(s)" text everywhere it
  appeared (home page status banner, topic/show/search pages).
- Topic 730's label was a mis-extracted book citation ("Jerome Jacobson
  (ed.). Studies in the archaeology of India and Pakistan...") that had also
  been canonicalized to the wrong Wikidata entity — a same-name collision
  with an unrelated archaeology book editor, not the actual McDonald's
  Monopoly fraud perpetrator these two episodes cover. Relabeled to
  "McDonald's Monopoly fraud" and pointed at Q16997479 (the closest real
  entity available; no dedicated fraud-specific Wikidata item exists).
  Data-only fix (topics table), not yet re-synced to the deployed instance.

## [0.6.1] - 2026-07-11

### Fixed

- The Docker build's known packaging gap (documented since 0.4.0): hark
  depends on adscrub as a local path dependency, but the build context only
  ever contained hark's own files, so `docker build .` couldn't resolve it.
  Fixed with the "multi-repo build script" option from docs/PLAN.md's open
  questions: `scripts/build-image.sh` stages git-archive-clean copies of both
  `hark/` and `adscrub/` side by side and builds against that directory;
  Dockerfile's COPY paths updated to match. `docker build .` run directly
  against this repo alone still won't work — use the script.

## [0.6.0] - 2026-07-11

### Added

- Per-show ad-stripping toggle (`shows.ad_stripping_enabled`, defaults on —
  matches the pipeline's previous unconditional behavior for every existing
  show). `hark chapters`/`transcribe`/`detect-ads`/`cut` now skip episodes
  belonging to disabled shows. Toggled from a button on each show's page.
- The show page now displays that show's ad-stripped feed URL
  (`/feed/<id>/<token>`) directly, so it can be copied into AntennaPod —
  previously it existed (every show gets a `feed_token`) but was never
  surfaced anywhere in the UI.

### Changed

- `cmd_detect_ads`/`cmd_cut` in cli.py now call adscrub's per-episode
  `detect_episode`/`cut_episode` directly in a hark-side loop instead of the
  bulk `detect_pending`/`cut_pending` orchestrators, so the per-show enabled
  filter actually takes effect (those bulk functions run their own internal
  pending-episode query with no way to restrict it to a specific episode
  set). Required adding `adscrub.detect.detect_episode` (see adscrub's own
  CHANGELOG) — `cut_episode` already existed.

## [0.5.0] - 2026-07-11

### Added

- Cross-show claims comparison, wired end to end: `hark compare` (live,
  Claude structured outputs via `claims.ClaudeComparator`) and
  `hark load-comparisons <file>` (pre-computed JSONL — same session-as-worker
  idiom as `hark load` for topic extraction). Every episode now has its own
  `/episode/<id>` page, linked from show/topic/search/home listings, showing
  — for each topic it covers — claims judged shared across shows vs. unique
  to one show's telling, or a specific reason none exists yet (not
  transcribed / only one show has covered it / transcribed by 2+ shows but
  not compared yet).
- `claims.py` (built additively in a previous session while `web.py`/`cli.py`
  were mid-merge from the adscrub port) is now fully wired in, now that
  merge has landed.

### Fixed

- `src/hark/__init__.py`'s `__version__` was still `"0.3.7"` even though
  `pyproject.toml` had already moved to `0.4.0` for the ad-stripping merge —
  the same stuck-constant bug class already caught and fixed in adscrub.
  `hark --version`, the CLI's outbound `User-Agent` header, and the web
  server's HTTP `Server` response header were all silently wrong; now track
  pyproject.toml.

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
