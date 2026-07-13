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

Origin: ideas #2 and #3 in the project-ideas tracker — see it for the full assessments
and reasoning. The ad-stripping
feature's own origin (AntennaPod's long-open feature request, why LLM-over-transcript
beats fingerprinting/crowdsourcing) is in adscrub's own repo history.

## Architecture decisions (already made — don't relitigate)

- Standalone service on the homelab, shaped like tiltmeter: scheduled ingest → pipeline →
  SQLite → API/UI. The owner's player stays AntennaPod.
- **Input integration:** AntennaPod syncs subscriptions + play history to Nextcloud (its gpodder
  sync app); hark reads from that API. OPML import as fallback. (Not wired yet —
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
- **Docker build:** hark's adscrub path dependency needs adscrub's source alongside this
  repo in the build context, which `docker build .`/`docker compose build` run from this
  repo alone can't provide. Resolved via `scripts/build-image.sh` (stages git-archive-clean
  copies of both repos into a temp directory, builds against that) — use it instead of
  `docker build .` directly. See docs/PLAN.md's ad-stripping section for the full story.
- **Multi-user (added 2026-07-13): shows/episodes/transcripts/ad_segments stay global,
  never per-user.** Only the subscription list (`user_shows`) and listen history
  (`listen_actions.user_id`) are per-account — that's the whole mechanism that keeps a
  show two accounts both subscribe to from being transcribed/ad-detected twice. Don't
  add a `user_id` to `shows`/`episodes`/`ad_segments` to "make it more multi-user" —
  that would defeat the entire point. `users` lives in auth.db, not hark.db (same
  session-survives-a-snapshot-restore reasoning as everything else in this file); `user_id`
  columns in hark.db are a soft cross-database reference, not an enforced FK. User
  management now has both a CLI (`hark user add/invite/list/remove`) and an admin-only
  `/admin/users` web page — the web page got added (2026-07-13, 0.15.0) once it turned
  out this project's own homelab deploy has no container shell access, so CLI-only
  wasn't actually usable day to day. Both call the same `Auth` methods; neither is more
  authoritative. `hark user invite`/`/admin/users` is the preferred onboarding path
  (single-use `/invite/<token>` link, scoped to one account) over `hark user add` (the
  shared-`$HARK_ADMIN_TOKEN` bootstrap, still supported but hands out a credential that
  also works on any other passwordless row). Non-admin accounts are capped at
  `gpodder_server.MAX_SHOWS_PER_USER` (10) — enforced on both paths that can add a
  subscription (`web.py`'s `subscribe()` and `record_subscription_changes()`), admin
  exempt. See docs/PLAN.md's multi-user and invite-links sections.

## Conventions

- Python 3.12+, `uv` + `pyproject.toml`, src layout. SQLite for storage. Keep dependencies
  minimal (feedparser/httpx-level, no frameworks until the API milestone).
- CHANGELOG.md in Keep a Changelog format; SemVer.
- **No AI/Claude attribution in commit messages** (no Co-Authored-By). Disclose AI use in the
  README instead. Commit messages describe actual changes, concise; never reference prompts
  or instructions.
- Significant multi-commit features go on a feature branch; small increments can go on main
  while the project is pre-0.1.
- Remote: **Gitea `flan/hark` is canonical** (origin, SSH) — always push there. Push to main
  is fine pre-0.2; also note the feature-branch rule above. The repo is public, and
  `claude-fleet`'s `jobs/repo-mirror.sh` mirrors it out to `github.com/sudolulo/hark` for
  visibility. GitHub is a read-only shop window: never push to it directly, and never treat
  it as a source of truth. Policy lives in `claude-fleet/config/repos.toml`. Do not add other
  remotes or mirrors unprompted.
- Public-facing docs must not link to `git.onetick.ninja` — outsiders cannot reach it.
  Cross-reference sibling projects by their GitHub URL.
