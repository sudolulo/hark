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
4. **Ad-stripping (merged in 2026-07-11):** fetches an episode, finds ad spans (free
   chapter-marker scan first, then Whisper transcription + LLM classification), cuts them
   out with `ffmpeg`, and re-hosts a clean feed — same "output integration" shape as #2,
   but covers *every* subscription, not just the genre-curated shows #1-#3 care about.
   Originated as a separate repo (`adscrub`) — merged once hark was going to use Whisper
   anyway and the two projects' schemas needed to converge. Full rationale, and everything
   ported/adapted, in docs/PLAN.md's "Ad-stripping merge" section — read that before
   touching `chapters.py`/`transcribe.py`/`detect.py`/`cut.py`/`podcast_feed.py`.

Origin: ideas #2 and #3 in a private ideas repo (git.onetick.ninja) — read
`~/project-ideas/README.md` for the full assessments and reasoning. The ad-stripping
feature's own origin (AntennaPod's long-open feature request, why LLM-over-transcript
beats fingerprinting/crowdsourcing) is in the adscrub repo's history — `flan/adscrub`
still exists but is superseded by this merge, not actively developed further.

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
- **Whisper transcription (added 2026-07-11, merged from adscrub):** `transcribe.py`'s
  model is cached process-wide and reloaded only on a `model_size` change. Ad-span detection
  and (later) episode-scoring fidelity should ask for the *same* model size and run
  sequentially through the cron-scheduled pipeline (they do) so exactly one Whisper model is
  ever resident in VRAM — this is a design discipline to maintain, not something merging the
  codebases guaranteed for free. GPU: `code` has a real RTX 2070 SUPER, Docker's `nvidia`
  runtime is registered; device picked at runtime via `ctranslate2.get_cuda_device_count()`
  (no torch dependency just for that check), CPU int8 fallback. `compose.gpu.yaml` requests
  the GPU explicitly for the Docker deploy; base install stays CPU-safe.
- **Feed/audio route auth:** `/feed/<show_id>/<token>` and `/audio/<episode_id>/<token>.<ext>`
  are unauthenticated (no cookie login — a podcast app can't do that) but gated by a random
  per-show `feed_token` embedded in the URL, compared with `secrets.compare_digest`. Not the
  dashboard's session system, and not wide open either — same idea as the tokened private-feed
  URLs most self-hosted podcast tools use. Revisit only if this ever needs to be reachable
  from outside a trusted network.

## Conventions

- Python 3.12+, `uv` + `pyproject.toml`, src layout. SQLite for storage. Dependencies grew
  with the ad-stripping merge (feedgen, faster-whisper) but stay deliberately lean — GPU
  runtime libs are an optional `gpu` extra, not a base dependency, and there's no torch
  anywhere (CUDA detection goes through `ctranslate2.get_cuda_device_count()` instead).
- CHANGELOG.md in Keep a Changelog format; SemVer.
- **No AI/Claude attribution in commit messages** (no Co-Authored-By). Disclose AI use in the
  README instead. Commit messages describe actual changes, concise; never reference prompts
  or instructions.
- Significant multi-commit features go on a feature branch (this repo is well past pre-0.1
  now — the adscrub merge itself went on `adscrub-merge`, not straight to main).
- Remote: private Gitea repo `flan/hark` (origin, SSH). Do not create additional remotes or
  mirrors unprompted.
