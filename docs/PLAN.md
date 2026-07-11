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
