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

## Ad-stripping merge (done, 0.4.0)

Not in the original milestone list — merged in from a separate repo (`adscrub`,
`flan/adscrub`) once two things became true: hark was going to use Whisper anyway
(see M4 below — no longer "not before"), and the ad-stripping pipeline needed to
cover *every* subscription, not just the genre-curated shows the topic index cares
about. That second point killed the earlier "keep them separate" reasoning — the
topic index and ad-stripping now legitimately want the same `shows`/`episodes`
data for overlapping-but-not-identical reasons, so one schema serving both beats
two schemas that would've needed the exact same rows duplicated across two
databases.

- Schema: `episodes` gains `chapters_url`/`chapters_scanned_at`/`transcript_path`/
  `llm_detected_at`/`cut_path`; `shows` gains `feed_token`; new `ad_segments` table.
  Additive migrations, backfilled — a pre-merge `hark.db` upgrades in place.
- Pipeline, cheapest-first: `hark chapters` (free — Podcasting 2.0 `<podcast:chapters>`
  keyword match) → `hark transcribe` (faster-whisper, GPU-auto-detected via
  `ctranslate2.get_cuda_device_count()`, no torch needed) → `hark detect-ads` (Claude
  structured outputs, model points at transcript segment indices not raw timestamps
  — avoids hallucinated boundaries) → `hark cut` (ffmpeg, `-c copy`, overlapping
  spans from any source merged before cutting — no "which source wins" rule needed).
  Every stage marks episodes done even on a zero-result outcome (a real bug in the
  original adscrub version — see its CHANGELOG 0.3.0 — fixed before the merge).
- Serving: `hark web`'s existing server also answers `/feed/<show_id>/<token>` and
  `/audio/<episode_id>/<token>.<ext>` — unauthenticated (a podcast app can't do the
  dashboard's cookie login) but token-gated per show, not wide open. This is new
  relative to adscrub's original design, which had no dashboard to integrate with.
- One shared `transcribe.py` / one cached Whisper model for both this pipeline and
  future M4 episode-scoring use — as long as both ask for the same model size and
  run sequentially (they do — cron-scheduled batch, not concurrent requests), only
  one model is ever resident in VRAM. A future scoring feature wanting a *different*
  model size would need to decide that as a real tradeoff, not get it for free.
- GPU: `code` physically has an RTX 2070 SUPER, Docker's `nvidia` runtime is
  registered — `compose.gpu.yaml` requests it; base install stays CPU-only-capable
  (`uv sync --extra gpu` pulls in cuBLAS/cuDNN only when actually deploying with
  passthrough).

## M2 — discovery

- Embedding similarity over episode topics → related shows, notable back-catalog episodes.
- Candidate-show pipeline: cheap signals first, deeper analysis only for shows that pass.

## M3 — AntennaPod loop

- Read subscriptions/history from Nextcloud gpodder sync (truenas).
- Generate custom RSS feeds as the recommendation delivery channel.
- Note: this is now also how new shows should reach the ad-stripping pipeline
  (currently still manual via `feeds.txt`/`hark resolve`, matching adscrub's original
  simpler `add-feed` flow) — wiring gpodder sync in properly is what actually
  delivers "every subscription gets ad-stripped," not just "every show you've typed
  into feeds.txt." Deliberately deferred rather than built as part of the merge —
  a real API integration project of its own, not something to build reflexively
  alongside the merge.

## M4 — episode scoring (tiltmeter-style)

- Defined interestingness metrics, calibration loop against owner ratings.
- Per-topic treatment comparison (depth, sensationalism) — needs transcripts for
  fidelity. Whisper is no longer "not before" — the ad-stripping merge above already
  wired `transcribe.py` in; this milestone reuses it rather than standing up its own.

## Seed shows (feeds.txt)

Start with well-known subject-per-episode shows across two genres, e.g.: Casefile,
Casual Criminalist, Swindled, The Rest Is History, Short History Of, Cautionary Tales.
Resolve their real feed URLs via iTunes Search API at runtime — do not hand-copy URLs.

## Open questions (owner input needed, don't block on these)

- ~~Hosting~~ Resolved 2026-07-10: private Gitea (`flan/hark`); revisit GitHub if it goes public.
- Which LLM/provider for extraction (M1 decision).
- ~~GPU/Whisper feasibility~~ Resolved 2026-07-11: `code` has a real GPU, Docker's
  `nvidia` runtime is registered, `compose.gpu.yaml` requests it — see the
  ad-stripping merge section above.
- `hark detect-ads` currently defaults to `claude-opus-4-8`; revisit cost vs.
  accuracy on ad-span boundaries once run against real transcripts.
- Real-world validation of chapters-URL parsing (`entry["podcast_chapters"]["url"]`)
  against an actual subscribed feed, not just the synthetic test fixture.
- When to actually wire the gpodder/Nextcloud subscription sync (M3) so ad-stripping
  covers real subscriptions instead of the manually-curated `feeds.txt` list.
