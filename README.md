# hark

Cross-podcast topic index and discovery service for subject-per-episode genres
(true crime, history, disasters, and the like), **plus** an ad-stripping proxy
that covers every subscription, not just the genre-curated ones. The topic
index resolves episodes to the real-world case/event/person they cover, so you
can ask "who covered the Dyatlov Pass incident?" and compare treatments across
shows; the ad-stripping side fetches an episode, finds ad spans (chapter
markers, or Whisper transcription + LLM classification), cuts them out with
`ffmpeg`, and re-hosts a clean feed the same podcast player subscribes to.

See `docs/PLAN.md` for milestones. Current state (0.4.0): feed resolution,
episode ingest, LLM topic extraction with Wikidata canonicalization, the
cross-show topic index, a full web UI, and the merged-in ad-stripping pipeline
(chapters/transcribe/detect-ads/cut/serve) — deployed live. M2 (discovery) is
still next for the topic-index side.

This merged in what was a separate repo (`adscrub`) once it became clear both
projects needed the same "fetch feed, re-host derived feed" shape and both
wanted Whisper — see docs/PLAN.md for why standalone stopped making sense.

## Usage

```
uv sync
uv run hark resolve            # feeds.txt show names -> feed URLs (iTunes Search API)
uv run hark ingest             # fetch feeds, upsert episodes (idempotent)
uv run hark extract --limit 20 # extract episode subjects (needs $ANTHROPIC_API_KEY)
uv run hark load batch.jsonl   # ingest pre-computed extractions (batch runs, no API key needed)
uv run hark canon              # retry Wikidata canonicalization for unmatched topics
uv run hark stats              # counts per show
uv run hark topics             # topics ranked by cross-show coverage
uv run hark who "dyatlov"      # who covered X (label substring or Wikidata QID)

# ad-stripping pipeline — works on any show, not just genre-curated ones
uv run hark chapters           # scan chapter markers for ad spans (free — no transcription)
uv run hark transcribe         # Whisper the rest
uv run hark detect-ads         # LLM ad-span classification (needs $ANTHROPIC_API_KEY)
uv run hark cut                # ffmpeg out the ad spans
```

The database defaults to `./hark.db`; override with `--db` or `$HARK_DB`.
Show names live in `feeds.txt`, one per line, `#` for comments.

## Web UI

`hark web` serves the topic index (default `0.0.0.0:8710`): a home dashboard
(coverage stats, genre breakdown, live indexing status, recently-indexed
feed), topic pages ("who covered X"), per-show pages, genre-filtered and
paginated topic browsing, and search. The whole site is behind a session
login wall; only `/login`, `/logout` and `/healthz` are open.
Bootstrap: set `HARK_ADMIN_TOKEN`, sign in as `admin` with that token, then set
a real password at `/account` (the token stops working once a password exists;
with neither set, login is impossible — fail-closed). Sessions live in a
separate `auth.db` (`--auth-db` / `$HARK_AUTH_DB`) so replacing `hark.db` with
a fresh data snapshot never logs anyone out. Set `HARK_COOKIE_SECURE=1` when
serving behind a TLS-terminating proxy.

The same server also answers `GET /feed/<show_id>/<token>` (the cleaned RSS
feed) and `GET /audio/<episode_id>/<token>.<ext>` (locally-cut episodes) —
deliberately *not* behind the login wall, since a podcast app can't do cookie
login. Instead each show gets a random `feed_token` (auto-generated,
`shows.feed_token`) that has to appear in the URL; wrong or missing token is a
404, not a redirect to `/login`. `--base-url`/`$HARK_BASE_URL` must be set to
wherever the podcast player can actually reach this server — it's embedded in
every generated audio link, and `serve` warns if left at the unreachable
`localhost` default.

In Docker: `docker compose up -d` (mounts `./data`, serves :8710); pipeline
stages run as one-shots, e.g. `docker compose run --rm hark ingest`.
Transcription runs CPU-only by default — see `compose.gpu.yaml` and CLAUDE.md
for the GPU deploy path (`uv sync --extra gpu` pulls in the cuBLAS/cuDNN libs
faster-whisper needs for CUDA).

Extraction calls the Anthropic API (default model `claude-opus-4-8`; override
with `--model` or `$HARK_MODEL`) and canonicalizes labels against Wikidata so
aliases merge ("BTK" = "Dennis Rader"). Runs are idempotent and resumable:
processed episodes are marked and skipped, failures are retried next run.
Ad-span classification is a separate model default (`--model`/`$HARK_AD_MODEL`)
since it's a differently-shaped task.

## Development

```
uv run pytest
```

Tests use local feed fixtures — no network.

## AI use disclosure

This project is developed with substantial assistance from AI coding tools
(Anthropic Claude). Design decisions and review are human; much of the code is
AI-written.
