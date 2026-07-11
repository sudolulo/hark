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
4. **Ad-stripping (added 2026-07-11):** finds ad spans (chapter markers, or Whisper +
   LLM classification) and cuts them out, covering *every* subscription, not just the
   genre-curated shows #1-#3 track. **This is provided by depending on `flan/adscrub` as
   a library, not by duplicating its code** — adscrub is a separate, standalone product.
   See "Architecture decisions" below before touching anything ad-stripping-related.

Origin: ideas #2 and #3 in a private ideas repo (git.onetick.ninja) — read
`~/project-ideas/README.md` for the full assessments and reasoning. The ad-stripping
feature's own origin (AntennaPod's long-open feature request, why LLM-over-transcript
beats fingerprinting/crowdsourcing) is in adscrub's own repo history.

## Architecture decisions (already made — don't relitigate)

- Standalone service on the homelab, shaped like tiltmeter: scheduled ingest → pipeline →
  SQLite → API/UI. The owner's player stays AntennaPod.
- **Input integration:** AntennaPod syncs subscriptions + play history to Nextcloud (gpodder
  sync app) on truenas; hark reads from that API. OPML import as fallback. (Not wired yet —
  see M3 in docs/PLAN.md; ad-stripping still uses the manual `feeds.txt`/`resolve` flow too.)
- **Output integration:** hark generates custom RSS feeds (e.g. "top episodes about topics
  you like", "best of candidate shows", ad-stripped versions of any subscription) that get
  subscribed to in AntennaPod like any podcast. No app modification anywhere.
- Feed URLs resolve via the keyless iTunes Search API; Podcast Index API can be added later
  (needs a registered key). Episode metadata comes from plain RSS.
- Topic extraction: LLM extraction from episode title/description, canonicalized against
  Wikidata — these genres name their subject in the metadata, so this doesn't need
  transcripts even though transcription is now available (see below).
- Topics can belong to multiple genres (Titanic = history + disaster); never force one bucket.
- **adscrub is a dependency, not a merge — this is deliberate and non-negotiable.**
  `flan/adscrub` is its own product: own repo, own schema, own CLI, deployable and useful
  standalone. hark depends on it (`[tool.uv.sources]` path dependency, editable — see
  pyproject.toml) and calls its functions directly. hark's `episodes`/`shows`/`ad_segments`
  schema is deliberately shaped to match adscrub's own column names *specifically so*
  adscrub's schema-coupled functions (`pending_episodes`, `scan_episode`,
  `transcribe_episode`, `detect_pending`, `cut_pending`, ...) work unchanged against hark's
  `conn` — call them from hark's cli.py directly. **Do not copy adscrub's source files into
  this repo.** That mistake was actually made once (2026-07-11), pushed to main, and had to
  be reverted via `git revert` once caught — see CHANGELOG 0.4.0. The only hark-owned
  ad-stripping code should be: the schema migration, cli.py's argparse wiring, and
  `podcast_feed.py` (genuinely schema-specific — adscrub's own feed-building code targets a
  different schema and has no token-auth concept, so it isn't reusable as-is).
- **Whisper transcription** (via adscrub) is cached process-wide, keyed by model size —
  hark's ad-span detection and (later) M4 episode-scoring should request the *same* model
  size and run sequentially through the cron-scheduled pipeline (they do) so exactly one
  model is ever resident in VRAM. This works automatically only because hark calls
  adscrub's actual `load_model()` function, not a copy — don't undermine it by ever adding a
  second, hark-owned copy of that caching logic. GPU: `code` has a real RTX 2070 SUPER,
  Docker's `nvidia` runtime is registered; `compose.gpu.yaml` requests it, hark's own `gpu`
  extra passes through to `adscrub[gpu]`.
- **Feed/audio route auth:** `/feed/<show_id>/<token>` and `/audio/<episode_id>/<token>.<ext>`
  are unauthenticated (no cookie login — a podcast app can't do that) but gated by a random
  per-show `feed_token` embedded in the URL, compared with `secrets.compare_digest`. Not the
  dashboard's session system, and not wide open either.
- **Known unsolved gap:** the adscrub path dependency doesn't resolve in the Docker build
  (build context only has hark's own files). Don't quietly work around this by copying
  adscrub's source into the build context or removing the dependency — it's a real packaging
  decision (git dependency + deploy key, vendored wheel, multi-repo build script) that needs
  to actually be made, not paved over. See docs/PLAN.md open questions.

## Conventions

- Python 3.12+, `uv` + `pyproject.toml`, src layout. SQLite for storage. Keep dependencies
  minimal (feedparser/httpx-level, no frameworks until the API milestone).
- CHANGELOG.md in Keep a Changelog format; SemVer.
- **No AI/Claude attribution in commit messages** (no Co-Authored-By). Disclose AI use in the
  README instead. Commit messages describe actual changes, concise; never reference prompts
  or instructions.
- Significant multi-commit features go on a feature branch; small increments can go on main
  while the project is pre-0.1.
- Remote: private Gitea repo `flan/hark` (origin, SSH). Push to main is fine pre-0.2; also
  note the feature-branch rule above. Do not create additional remotes or mirrors unprompted.
