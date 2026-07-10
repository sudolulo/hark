# hark

Cross-podcast topic index and discovery service for subject-per-episode genres
(true crime, history, disasters, and the like). The goal: resolve episodes to the
real-world case/event/person they cover, so you can ask "who covered the Dyatlov
Pass incident?" and compare treatments across shows.

See `docs/PLAN.md` for milestones. Current state (0.1.0 / M0): feed resolution and
episode ingest into SQLite. Topic extraction lands in M1.

## Usage

```
uv sync
uv run hark resolve    # feeds.txt show names -> feed URLs (iTunes Search API)
uv run hark ingest     # fetch feeds, upsert episodes (idempotent)
uv run hark stats      # counts per show
```

The database defaults to `./hark.db`; override with `--db` or `$HARK_DB`.
Show names live in `feeds.txt`, one per line, `#` for comments.

## Development

```
uv run pytest
```

Tests use local feed fixtures — no network.

## AI use disclosure

This project is developed with substantial assistance from AI coding tools
(Anthropic Claude). Design decisions and review are human; much of the code is
AI-written.
