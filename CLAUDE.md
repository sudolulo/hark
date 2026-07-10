# hark

Cross-podcast topic index and discovery service. Working title "hark" — renaming is cheap,
don't get attached.

## What this is

A homelab web service (NOT a mobile app, NOT an AntennaPod fork) that:

1. **Topic index (first milestone):** resolves podcast episodes in subject-per-episode genres
   (true crime, history, disasters, scams/fraud, biographies, espionage, cults, mysteries) to
   the real-world case/event/person they cover, so you can ask "who covered the Dyatlov Pass
   incident?" and compare treatments across shows.
2. **Discovery:** related-show and notable-episode recommendations via topic/embedding
   similarity.
3. **Episode scoring (later):** metric-based interestingness ratings, tiltmeter-style
   (auditable, defined metrics, calibrated against the owner's actual listening).

Origin: ideas #2 and #3 in a private ideas repo (git.onetick.ninja) — read
`~/project-ideas/README.md` for the full assessments and reasoning.

## Architecture decisions (already made — don't relitigate)

- Standalone service on the homelab, shaped like tiltmeter: scheduled ingest → pipeline →
  SQLite → API/UI. The owner's player stays AntennaPod.
- **Input integration:** AntennaPod syncs subscriptions + play history to Nextcloud (gpodder
  sync app) on truenas; hark reads from that API. OPML import as fallback. (Not wired in M0.)
- **Output integration:** hark generates custom RSS feeds (e.g. "top episodes about topics
  you like", "best of candidate shows") that get subscribed to in AntennaPod like any podcast.
  No app modification anywhere.
- Feed URLs resolve via the keyless iTunes Search API; Podcast Index API can be added later
  (needs a registered key). Episode metadata comes from plain RSS.
- Topic extraction: LLM extraction from episode title/description, canonicalized against
  Wikidata. Transcripts/Whisper are explicitly OUT of scope until much later — these genres
  name their subject in the metadata.
- Topics can belong to multiple genres (Titanic = history + disaster); never force one bucket.

## Conventions

- Python 3.12+, `uv` + `pyproject.toml`, src layout. SQLite for storage. Keep dependencies
  minimal (feedparser/httpx-level, no frameworks until the API milestone).
- CHANGELOG.md in Keep a Changelog format; SemVer.
- **No AI/Claude attribution in commit messages** (no Co-Authored-By). Disclose AI use in the
  README instead. Commit messages describe actual changes, concise; never reference prompts
  or instructions.
- Significant multi-commit features go on a feature branch; small increments can go on main
  while the project is pre-0.1.
- No remote configured yet — do NOT create forge repos or add remotes; the owner will decide
  hosting.
