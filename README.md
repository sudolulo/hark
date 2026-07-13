# hark

Cross-podcast topic index and discovery service for subject-per-episode genres
(true crime, history, disasters, and the like). The goal: resolve episodes to the
real-world case/event/person they cover, so you can ask "who covered the Dyatlov
Pass incident?" and compare treatments across shows.

hark also strips ads from every subscription (not just the genre-curated shows
above) by depending on [adscrub](https://git.onetick.ninja/flan/adscrub) — a
separate, standalone product — as a library: `hark chapters`/`transcribe`/
`detect-ads`/`cut` call straight into adscrub's functions, since hark's own
`episodes`/`ad_segments` schema was deliberately shaped to match adscrub's, so
adscrub's schema-coupled functions work unchanged against hark's database.
Nothing here is a copy of adscrub's code — see CLAUDE.md and docs/PLAN.md for
the integration design and why it's a dependency, not a merge. Ad-stripping is
per-show (on by default) — toggle it from that show's page, which also shows
the feed URL to subscribe to in AntennaPod once you're ready.

Once a topic has transcripts from 2+ shows (from the ad-stripping pipeline's
transcription step), hark can also compare what each show actually claimed —
shared facts vs. claims unique to one show's telling — shown on every
episode's own page.

hark is multi-user: each account has its own subscription list and listen
history, but the show catalog and everything the pipeline produces
(transcripts, ad spans, topic extraction) stay global and shared — a show two
accounts both subscribe to only gets processed once, not once per subscriber.
Point each person's AntennaPod at hark's gpodder-sync endpoint with their own
login and they see only their own subscriptions/history. Non-admin accounts
are capped at 10 podcasts (enforced the same way whether the show gets added
via AntennaPod sync or the web UI); the admin account is exempt and can also
toggle the two settings that are genuinely shared across everyone —
ad-stripping and topic-index enablement per show.

Inviting someone: `hark user invite <name>` (or the `/admin/users` web page)
creates an account with a one-time `/invite/<token>` link — send it to them,
they set their own password and land straight in the dashboard. No shared
secret changes hands; the link only works for that one account and expires
after a week.

The deployed instance runs its own pipeline unattended: subscription sync, ingest,
canonicalization, chapter-scanning, transcription, and ad-cutting all run on a
schedule with no manual steps. Topic extraction, claims comparison, and ad-span
detection — the three steps that need real judgment, not just fetching/matching —
run the same way but as a scheduled Claude agent instead of a paid API call
(`claude-fleet`'s `jobs/agents/hark-pipeline.md`); this project has never used
`$ANTHROPIC_API_KEY` and isn't starting now. See docs/PLAN.md's "Deployed pipeline
automation" section.

See `docs/PLAN.md` for milestones. Current state (0.14.0): feed resolution,
episode ingest, LLM topic extraction with Wikidata canonicalization, the
cross-show topic index, a full web UI, adscrub-backed ad-stripping (with a
per-show on/off toggle and feed URL, both from the show page; ad-span
detection is now session-as-X automated too, not just chapter-marker
scanning), cross-show claims comparison, M2 discovery (related shows/topics
by co-occurrence, candidate-show search, and an interim notable-episodes
page), M3's AntennaPod loop (Nextcloud gpodder subscription + listen-history
sync, OPML import fallback, and hark speaking the gpodder-sync protocol
itself so AntennaPod can point directly at it — no app fork needed), a
per-show topic-index toggle (new shows start excluded from extraction until
reviewed — most subscriptions aren't subject-per-episode genre shows), and
multi-user accounts (per-user subscription lists + listen history, shared
processing) — deployed live.

## Demo

The dashboard: coverage stats, genre breakdown, and the most-covered topics
across shows.

![hark dashboard](docs/screenshots/dashboard.png)

A topic page — every episode across every show that covers it:

![Somerton Man topic page](docs/screenshots/topic.png)

The actual point of this project: once 2+ shows have transcripts for the same
topic, hark diffs what they each said — shared facts vs. claims unique to one
show's telling — right on the episode page.

![claims comparison on an episode page](docs/screenshots/episode-claims.png)

A show page: ad-stripped feed URL to subscribe to in AntennaPod, the per-show
on/off toggle, and related shows by topic overlap.

![show page](docs/screenshots/show.png)

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

# M3: subscriptions/history from Nextcloud's GPodder Sync app, or an OPML export
uv run hark sync-subscriptions --nextcloud-url https://host:9001 \
  --nextcloud-user U --nextcloud-password P   # register new shows from subscriptions
uv run hark sync-history --nextcloud-url ...  # play-history events, for future M4 scoring
uv run hark import-opml export.opml           # same show-registration, from a file instead

# M2: candidate shows not yet tracked (report-only unless --add)
uv run hark discover --genre true_crime --add

# ad-stripping pipeline (backed by the adscrub library) — every show, not just feeds.txt's
uv run hark chapters           # scan chapter markers for ad spans (free — no transcription)
uv run hark transcribe         # Whisper the rest
uv run hark transcribe --cross-show-only  # priority subset: episodes on topics 2+ shows cover
uv run hark detect-ads         # LLM ad-span classification (needs $ANTHROPIC_API_KEY)
uv run hark load-ad-detections out.jsonl  # pre-computed (batch runs, no API key needed)
uv run hark cut                # ffmpeg out the ad spans
uv run hark fsck --fix         # clear transcript_path pointers whose file no longer exists

# cross-show claims comparison — once a topic has 2+ shows' transcripts
uv run hark compare                    # live, needs $ANTHROPIC_API_KEY
uv run hark load-comparisons out.jsonl # pre-computed (batch runs, no API key needed)

# multi-user accounts (auth.db only — no --db)
uv run hark user invite alice [--admin]  # preferred: prints a one-time /invite/<token> link
uv run hark user add alice [--admin]     # bootstraps via the shared $HARK_ADMIN_TOKEN instead
uv run hark user list                    # shows any still-pending invite links too
uv run hark user remove alice
```

The database defaults to `./hark.db`; override with `--db` or `$HARK_DB`.
Show names live in `feeds.txt`, one per line, `#` for comments.

## Setup

adscrub is a **path dependency** (`../adscrub`, editable — see
`pyproject.toml`'s `[tool.uv.sources]`), so `flan/adscrub` needs to be checked
out as a sibling of this repo before `uv sync` will resolve it:

```
cd .. && git clone ssh://git@git.onetick.ninja:55214/flan/adscrub.git
cd hark && uv sync
```

For local development, that's all you need. Plain `docker build .`/`docker compose
build` run from this repo alone still won't work, though — the build context only has
hark's own files, and the adscrub path dependency needs adscrub's source alongside it.
Use `scripts/build-image.sh` instead (stages git-archive-clean copies of both repos
into a temp directory and builds against that) — see docs/PLAN.md's ad-stripping
section for the full story.

## Web UI

`hark web` serves the topic index (default `0.0.0.0:8710`): a home dashboard
(coverage stats, genre breakdown, live indexing status, ad-stripping/claims-
comparison pipeline status, recently-indexed feed — shared, same for every
account), topic pages ("who covered X"), per-show pages (episode list,
subscribe/unsubscribe, per-show pipeline progress, related shows — the
ad-stripping and topic-index toggles are admin-only, since those are global
settings shared across every account, not personal preference), a `/shows`
list defaulting to your own subscriptions (`?all=1` browses the full
catalog) and flagging any not yet reviewed for the topic index,
genre-filtered and paginated topic browsing, an interim `/notable` page
(most-contested claims comparisons, rarest-genre episodes), and search. The
whole site is behind a session login wall; only `/login`, `/logout`,
`/invite/<token>` and `/healthz` are open. Bootstrap: set `HARK_ADMIN_TOKEN`,
sign in as `admin` with that token, then set a real password at `/account`
(the token stops working once a password exists; with neither set, login is
impossible — fail-closed). Multi-user: `/admin/users` (admin-only) or `hark
user invite` creates further accounts with a one-time invite link — see
Usage above; `hark user add` (the shared-token bootstrap) still works too.
Sessions live in a separate `auth.db` (`--auth-db` / `$HARK_AUTH_DB`) so
replacing `hark.db` with a fresh data snapshot never logs anyone out — but
note that per-account subscription lists (`user_shows`) live in *hark.db*,
not auth.db, so they follow hark.db's own data, not the account. Set
`HARK_COOKIE_SECURE=1` when serving behind a TLS-terminating proxy.

The same server also answers `GET /feed/<show_id>/<token>` (the cleaned RSS
feed) and `GET /audio/<episode_id>/<token>.<ext>` (locally-cut episodes) —
deliberately *not* behind the login wall, since a podcast app can't do cookie
login. Instead each show gets a random `feed_token` (auto-generated,
`shows.feed_token`) that has to appear in the URL; wrong or missing token is a
404, not a redirect to `/login`. `--base-url`/`$HARK_BASE_URL` must be set to
wherever the podcast player can actually reach this server — it's embedded in
every generated audio link, and `web` warns if left at the unreachable
`localhost` default.

It also answers the gpodder-sync protocol AntennaPod's own "Nextcloud" sync
setting speaks (`/index.php/apps/gpoddersync/...`, HTTP Basic Auth) — point
AntennaPod at hark's base URL as if it were a Nextcloud instance and
subscriptions/listen-history sync directly, no Nextcloud (or app fork)
required. Multi-user: each person's AntennaPod authenticates as their own
hark account and only ever sees that account's own subscriptions/history —
the show catalog itself stays shared, so two accounts subscribed to the same
show still only cause it to be processed once. See `gpodder_server.py` and
`docs/PLAN.md`'s M3 and multi-user sections for the protocol/schema details.

In Docker: `docker compose up -d` (mounts `./data`, serves :8710); pipeline
stages run as one-shots, e.g. `docker compose run --rm hark ingest`.
Transcription runs CPU-only by default — see `compose.gpu.yaml` and CLAUDE.md
for the GPU deploy path. Set `HARK_NEXTCLOUD_URL`/`_USER`/`_PASSWORD` (and
`HARK_NEXTCLOUD_INSECURE=1` for a self-signed cert — see `--nextcloud-insecure`
above) to enable `sync-subscriptions`/`sync-history` in a scheduled pipeline
run; without them M3 sync is simply skipped, same fail-soft shape as
`$ANTHROPIC_API_KEY` being unset for `extract`/`compare`.

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
