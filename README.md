# hark

Cross-podcast topic index and discovery service for subject-per-episode genres
(true crime, history, disasters, and the like). The goal: resolve episodes to the
real-world case/event/person they cover, so you can ask "who covered the Dyatlov
Pass incident?" and compare treatments across shows.

See `docs/PLAN.md` for milestones. Current state (0.3.5, M1 complete): feed
resolution, episode ingest, LLM topic extraction with Wikidata canonicalization,
the cross-show topic index, and a full web UI — deployed live. M2 (discovery) is
next.

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

In Docker: `docker compose up -d` (mounts `./data`, serves :8710); pipeline
stages run as one-shots, e.g. `docker compose run --rm hark ingest`.

Extraction calls the Anthropic API (default model `claude-opus-4-8`; override
with `--model` or `$HARK_MODEL`) and canonicalizes labels against Wikidata so
aliases merge ("BTK" = "Dennis Rader"). Runs are idempotent and resumable:
processed episodes are marked and skipped, failures are retried next run.

## Development

```
uv run pytest
```

Tests use local feed fixtures — no network.

## AI use disclosure

This project is developed with substantial assistance from AI coding tools
(Anthropic Claude). Design decisions and review are human; much of the code is
AI-written.
