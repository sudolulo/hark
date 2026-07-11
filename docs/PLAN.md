# hark — plan

Milestones. Each one ships something usable and gets a CHANGELOG version.

## M0 — scaffold + ingest (done, 0.1.0)

- Project scaffold: uv/pyproject, src layout, pytest.
- SQLite schema: shows, episodes, topics, episode_topics (extraction fields nullable —
  populated in M1).
- Feed resolution: show names in `feeds.txt` → feed URLs via iTunes Search API (keyless).
- RSS ingest: fetch + parse feeds, upsert shows/episodes (id, title, description, pubdate,
  duration, audio URL). Idempotent re-runs.
- CLI: `hark resolve`, `hark ingest`, `hark stats`.
- Unit tests with feed fixtures (no network in tests).

## M1 — topic extraction + index (done, 0.2.0)

- LLM extraction of subject entities from title/description (stub interface in M0; model
  wiring decided when we get here).
  - Wired to the Anthropic API (`claude-opus-4-8` default, `--model`/`$HARK_MODEL` to
    override, e.g. `claude-haiku-4-5` for cheap runs). In practice no `$ANTHROPIC_API_KEY`
    was ever available on the dev box, so the ~2,200-episode backfill was done by hand:
    a Claude session acted as the extractor directly, writing structured extraction JSONL
    across 22 batches, ingested via the new `hark load` command (same canonicalize+store
    path as `hark extract`, just fed pre-computed records instead of calling the API).
- Canonicalization against Wikidata (aliases: "BTK" = "Dennis Rader"); multi-part/serial
  episode handling; multi-genre topics. `hark canon` retries unmatched topics.
- Topic pages: "who covered X" — the core query. Shipped as both CLI (`hark who`,
  `hark topics`) and web UI (see below) — not CLI-only as originally planned.

## Web UI + deployment (done, 0.3.0–0.3.5)

Not in the original milestone list — added mid-stream on explicit request, ahead of M2.

- Dependency-free stdlib web frontend (`hark web`): home dashboard (coverage stats, genre
  breakdown, live indexing-status banner, recently-indexed feed), topic pages, per-show
  pages, genre-filtered and paginated topic listing, search, account/session management.
- Security model mirrors `~/influence-registry`: session auth, HttpOnly/SameSite cookies,
  stretched+salted passwords, fail-closed bootstrap via `HARK_ADMIN_TOKEN`.
- Docker packaging (Dockerfile + compose.yaml) mirrors `~/tiltmeter`'s pattern; runs as
  uid/gid 568 via a root→chown→`gosu` entrypoint.
- Deployed live on the homelab TrueNAS box as a custom app.
- Two full audit passes (security + code-quality, then a screenshot-driven UX pass against
  the real dataset) — see CHANGELOG 0.3.1 and 0.3.5 for what each one caught.

## Ad-stripping via adscrub (done, 0.4.0)

Not in the original milestone list. `flan/adscrub` is a separate, standalone
product (its own repo, schema, CLI, deployable alone) that does chapter-marker
scanning, Whisper transcription, LLM ad-span classification, and ffmpeg
cutting. hark depends on it **as a library** rather than duplicating its code
— two products, not a merge (an earlier session actually did a full code
merge here; it was reverted per explicit correction — see CHANGELOG 0.4.0 and
CLAUDE.md for why, and don't repeat that mistake).

- **Why a dependency, not two fully separate schemas:** hark's own
  `episodes` gained `chapters_url`/`chapters_scanned_at`/`transcript_path`/
  `llm_detected_at`/`cut_path`, and `shows` gained `feed_token`; new
  `ad_segments` table. These were deliberately shaped to match adscrub's own
  schema column-for-column, so adscrub's schema-coupled functions
  (`pending_episodes`, `scan_episode`, `transcribe_episode`, `detect_pending`,
  `cut_pending`, ...) work **unchanged** against hark's own `conn` — hark's
  CLI (`chapters`/`transcribe`/`detect-ads`/`cut`) calls them directly. No
  hark-side `chapters.py`/`transcribe.py`/`detect.py`/`cut.py` files exist;
  that would just be duplicated code with its own drift risk.
- **What's genuinely hark's own code:** the CLI wiring (cli.py), the schema
  migration, and `podcast_feed.py` (feed-building — adscrub's own `feed.py`
  targets its `feeds`/`feed_id` schema and has no token concept, so this one
  isn't reusable as-is; not worth generalizing adscrub's version for one
  consumer).
- **Serving:** `hark web` also answers `/feed/<show_id>/<token>` and
  `/audio/<episode_id>/<token>.<ext>` — unauthenticated (a podcast app can't
  do the dashboard's cookie login) but gated by a random per-show
  `feed_token`, not wide open.
- **One shared Whisper model:** adscrub's `transcribe.load_model()` caches
  one model process-wide, keyed by model size. Since hark's CLI calls that
  same function (not a copy), ad-span detection and future M4 episode-scoring
  share one cached instance for free — *as long as* both ask for the same
  model size and run sequentially (they do — cron-scheduled batch, not
  concurrent requests). A future scoring feature wanting a genuinely
  different model size would need to decide that as a real tradeoff.
- **GPU:** `code` has a real RTX 2070 SUPER, Docker's `nvidia` runtime is
  registered; `compose.gpu.yaml` requests it, and hark's own `gpu` extra just
  passes through to `adscrub[gpu]` rather than duplicating the cuBLAS/cuDNN
  package list.
- **Known gap, not solved:** the path dependency (`../adscrub`, editable)
  only works for local dev. The Docker build context doesn't include
  adscrub's source, so `docker compose build` doesn't actually work yet —
  needs a real packaging decision (git dependency + deploy key, vendoring a
  built wheel into the build context, or a small multi-repo build script).
  See open questions below.

## M2 — discovery

- Embedding similarity over episode topics → related shows, notable back-catalog episodes.
- Candidate-show pipeline: cheap signals first, deeper analysis only for shows that pass.

## M3 — AntennaPod loop

- Read subscriptions/history from Nextcloud gpodder sync (truenas).
- Generate custom RSS feeds as the recommendation delivery channel.
- Note: this is also how new shows should reach the ad-stripping pipeline
  (currently manual via `feeds.txt`/`hark resolve`) — wiring gpodder sync in
  properly is what actually delivers "every subscription gets ad-stripped,"
  not just "every show you've typed into feeds.txt." Deliberately deferred,
  not built alongside the adscrub dependency work — a real API integration
  project of its own.

## M4 — episode scoring (tiltmeter-style)

- Defined interestingness metrics, calibration loop against owner ratings.
- Per-topic treatment comparison (depth, sensationalism) — needs transcripts for
  fidelity. Whisper is already available via the adscrub dependency (see above);
  this milestone should call `adscrub.transcribe`'s functions the same way the
  ad-stripping pipeline does, not stand up its own transcription path.

## Seed shows (feeds.txt)

Start with well-known subject-per-episode shows across two genres, e.g.: Casefile,
Casual Criminalist, Swindled, The Rest Is History, Short History Of, Cautionary Tales.
Resolve their real feed URLs via iTunes Search API at runtime — do not hand-copy URLs.

## Open questions (owner input needed, don't block on these)

- ~~Hosting~~ Resolved 2026-07-10: private Gitea (`flan/hark`); revisit GitHub if it goes public.
- Which LLM/provider for extraction (M1 decision).
- ~~GPU/Whisper feasibility~~ Resolved 2026-07-11: `code` has a real GPU, Docker's
  `nvidia` runtime is registered — see the adscrub dependency section above.
- **How to make the adscrub path dependency work in the Docker build.** Not solved;
  the build context currently only has hark's own files. Options: git dependency
  (needs a deploy key baked into the build, or a build secret), vendor a built
  adscrub wheel into the build context, or a small script that builds both repos
  together. Don't guess at this — it's a real infra decision.
- `hark detect-ads` currently defaults to `claude-opus-4-8`; revisit cost vs.
  accuracy on ad-span boundaries once run against real transcripts.
- When to actually wire the gpodder/Nextcloud subscription sync (M3) so ad-stripping
  covers real subscriptions instead of the manually-curated `feeds.txt` list.
