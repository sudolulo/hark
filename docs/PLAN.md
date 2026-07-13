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

## Ad-stripping via adscrub (done, 0.4.0)

Not in the original milestone list. `flan/adscrub` is a separate, standalone
product (its own repo, schema, CLI, deployable alone) that does chapter-marker
scanning, Whisper transcription, LLM ad-span classification, and ffmpeg
cutting. hark depends on it **as a library** rather than duplicating its code
— two products, not a merge (an earlier session actually did a full code
merge here; it was reverted per explicit correction — see CHANGELOG 0.4.0 and
CLAUDE.md for why, and don't repeat that mistake).

- **Why a dependency, not two fully separate schemas:** hark's own
  `episodes` gained `chapters_url`/`chapters_scanned_at`/`transcript_path`/
  `llm_detected_at`/`cut_path`, and `shows` gained `feed_token`; new
  `ad_segments` table. These were deliberately shaped to match adscrub's own
  schema column-for-column, so adscrub's schema-coupled functions
  (`pending_episodes`, `scan_episode`, `transcribe_episode`, `detect_pending`,
  `cut_pending`, ...) work **unchanged** against hark's own `conn` — hark's
  CLI (`chapters`/`transcribe`/`detect-ads`/`cut`) calls them directly. No
  hark-side `chapters.py`/`transcribe.py`/`detect.py`/`cut.py` files exist;
  that would just be duplicated code with its own drift risk.
- **What's genuinely hark's own code:** the CLI wiring (cli.py), the schema
  migration, and `podcast_feed.py` (feed-building — adscrub's own `feed.py`
  targets its `feeds`/`feed_id` schema and has no token concept, so this one
  isn't reusable as-is; not worth generalizing adscrub's version for one
  consumer).
- **Serving:** `hark web` also answers `/feed/<show_id>/<token>` and
  `/audio/<episode_id>/<token>.<ext>` — unauthenticated (a podcast app can't
  do the dashboard's cookie login) but gated by a random per-show
  `feed_token`, not wide open.
- **One shared Whisper model:** adscrub's `transcribe.load_model()` caches
  one model process-wide, keyed by model size. Since hark's CLI calls that
  same function (not a copy), ad-span detection and future M4 episode-scoring
  share one cached instance for free — *as long as* both ask for the same
  model size and run sequentially (they do — cron-scheduled batch, not
  concurrent requests). A future scoring feature wanting a genuinely
  different model size would need to decide that as a real tradeoff.
- **GPU:** `code` has a real RTX 2070 SUPER, Docker's `nvidia` runtime is
  registered; `compose.gpu.yaml` requests it, and hark's own `gpu` extra just
  passes through to `adscrub[gpu]` rather than duplicating the cuBLAS/cuDNN
  package list.
- **Docker packaging gap: resolved (0.6.1)**, via the "small multi-repo build
  script" option. `scripts/build-image.sh` stages git-archive-clean copies of
  `hark/` and `adscrub/` (tracked files only) side by side into a temp
  directory and builds against that as the Docker context; Dockerfile's COPY
  paths were updated to expect that layout (`adscrub` at the context root,
  hark's own files under `hark/`). Plain `docker build .`/`docker compose
  build` run from this repo alone still won't work — the script is the real
  entry point now, `compose.yaml`'s `build:` directive was removed since it
  was never functional to begin with.
- **Per-show toggle + feed URL (0.6.0):** the pipeline originally ran
  unconditionally against every show — no way to exclude a show you don't
  want transcribed (real compute per episode). `shows.ad_stripping_enabled`
  (defaults on) gates `chapters`/`transcribe`/`detect-ads`/`cut`; toggled
  from a button on the show page, which also now displays that show's
  `/feed/<id>/<token>` URL directly (it always existed — every show gets a
  `feed_token` — but was never shown anywhere before this). Required adding
  `adscrub.detect.detect_episode` (a public per-episode function, matching
  `transcribe_episode`/`cut_episode`'s existing shape) since `detect_pending`/
  `cut_pending`'s bulk orchestration has no hook for hark to restrict which
  episodes get processed — hark's CLI now calls the per-episode functions in
  its own loop instead for `detect-ads`/`cut` (same as it already did for
  `transcribe`).
- **Note on where this is configured:** the toggle lives in `hark.db`
  (alongside `feed_token` — same category of per-show config), not `auth.db`.
  That means, like every other value in `hark.db`, it's only durable on
  whichever host is the source of truth for pipeline data — see the "Deploying
  the app container does NOT deploy its data" note in the deploy runbook. If
  the deployed instance's `hark.db` gets wholesale-replaced by a fresh sync
  from the pipeline host, a toggle set only on the deployed site would be
  lost unless also set on the source side.

## Cross-show claims comparison (done, 0.5.0)

Not in the original milestone list either — the natural follow-up to "who
covered X" once transcripts exist: what did each show actually *say*, and
where do their tellings agree or diverge? Lives in `claims.py`, built
additively in a separate session while `web.py`/`cli.py` were mid-merge from
the adscrub port, wired in fully once that merge landed.

- **Comparison, not a raw diff:** a literal text diff between independently
  scripted episodes is close to useless. Instead the model gets all of a
  topic's transcribed episodes in one call and returns which claims are
  shared across shows vs. unique to one show's telling — same structured-
  outputs idiom as `extract.py`/`detect.py`.
- **Own table, not a db.py schema change:** `topic_comparisons`, created via
  its own `ensure_schema()` (same idiom as `web.py`'s `Auth` for `auth.db`) —
  avoided touching the shared schema while it was in flux.
- **Two ways to run it**, same split as topic extraction: `hark compare`
  (live, `ClaudeComparator`, needs `$ANTHROPIC_API_KEY`) or
  `hark load-comparisons <file>` (pre-computed JSONL — used the first time,
  session-as-comparator, no API key).
- **`/episode/<id>`** (new route) shows, per topic the episode covers: shared
  claims, claims unique to each show (the episode's own show labeled
  "(this episode)"), or a specific reason nothing's there yet (not
  transcribed / only one show has covered it so far / 2+ shows transcribed
  but not compared yet). Linked from every episode-title cell across the
  site (show/topic/search/home) — a page with no inbound links doesn't get
  used, per the earlier UI-audit lesson about discoverability.
- **Read-only-connection-safe:** `get_comparison()` deliberately skips
  `ensure_schema()` so it works against `web.py`'s read-only `App.db()`
  connection even before `topic_comparisons` exists (SQLite's `CREATE TABLE
  IF NOT EXISTS` still needs write access to *create* it, but is a no-op —
  and safe on a read-only connection — once it already exists).

## M2 — discovery

- **Related shows (done, first cut, 0.7.0):** each show page lists other shows ranked by
  shared topic count (`web.related_shows()`) — a topic-co-occurrence stand-in for the
  originally planned embedding similarity, using data M1 extraction already produces
  rather than standing up a separate embedding model/API key. 171 topics already have 2+
  show coverage across the full 2199-episode corpus, enough for the ranking to be
  meaningful (e.g. Casefile True Crime ↔ The Casual Criminalist: 63 shared topics).
  Revisit with real embeddings if co-occurrence ever proves too coarse (e.g. it can't
  distinguish "both cover serial killers" from "both cover the *same* serial killer" the
  way claims comparison's per-topic transcript reading can).
- **Related topics (done, first cut, 0.8.0):** same idiom one level down — each topic
  page lists other topics that co-occur in the same episodes (`web.related_topics()`),
  ranked by shared episode count. Caught a real data bug in the process: topic 236's
  label was "Fred Wes" — not a hark extraction error, but a typo in Wikidata's own
  entity label for the correct QID, mirrored faithfully into hark's own `topics.label`.
  Corrected locally; the case for this pattern (a name showing up truncated/misspelled
  purely because the *upstream* Wikidata label is wrong) is worth remembering if it
  recurs — check the actual Wikidata entity before assuming hark's extraction is at
  fault.
- **Notable back-catalog episodes (done, interim, 0.10.0):** `/notable` — explicitly
  labeled provisional, not M4's real interestingness scoring. Two distinct signals that
  don't just repeat the home page's cross-show-coverage ranking: "most contested" (topics
  with a loaded claims comparison, ranked by how many claims are unique to one show rather
  than shared — `web.contested_topics()`) and "rare coverage" (episodes in hark's two
  least-common genres by topic count — `web.rare_genre_episodes()`). Revisit once M4 ships
  real metrics; this page's framing should shift from "here's what we can derive" to
  "here's what's actually rated highly."
- **Candidate-show pipeline (done, first cut, 0.10.0):** `hark discover [--genre G] [--add]`
  — cheap signal is an iTunes Search sweep over a curated seed-term list per hark genre
  (`discover.SEED_TERMS`), deduplicated against already-tracked shows by feed_url. Reports
  candidates ranked by episode count; report-only by default, `--add` registers them
  (same `resolve.add_show_by_feed_url()` bare-row path as sync-subscriptions/import-opml).
  "Deeper analysis only for shows that pass" isn't a second automated stage — it's the
  owner's review of the reported list, then the existing ingest/extract pipeline once a
  candidate is actually added, same as any other show.

## M3 — AntennaPod loop (done, 0.10.0)

- **Subscriptions:** `hark sync-subscriptions` reads Nextcloud's GPodder Sync app
  (`nextcloud.py`) — `GET /index.php/apps/gpoddersync/subscriptions` returns a full
  add/remove history, not a live snapshot; "currently subscribed" is `add − remove`.
  Registers any feed URL not already a known show (bare row, same as OPML import —
  title/description/image get filled in by the next `hark ingest`). Deliberately never
  removes a show on gpodder-side unsubscribe: hark's topic index is meant to be a durable
  "who covered X" record independent of current subscription state.
- **Listen history:** `hark sync-history` pulls `episode_action` events (play position,
  timestamps) into a new `listen_actions` table — nothing reads it yet, it's there for M4's
  "calibrated against the owner's actual listening" scoring. Incremental via a stored
  cursor (`sync_state` table + GPodder Sync's own `since=<timestamp>` param), since this
  list only grows (2935 actions in the real account at the time this was built) — a full
  refetch every run would be wasteful.
- **OPML import fallback:** `hark import-opml <file>` — same `add_show_by_feed_url()` path,
  for a one-off OPML export instead of (or alongside) the live Nextcloud account.
- **TLS:** the deployed Nextcloud instance uses a self-signed cert (LAN-only service) —
  `--nextcloud-insecure`/`$HARK_NEXTCLOUD_INSECURE` opts out of verification for that one
  connection specifically (`make_nextcloud_client()`, separate from `make_client()`, which
  stays fully verifying for every other caller: Anthropic, iTunes, HF Hub, podcast feed
  hosts). Defaults to verifying; the deployed container sets the opt-out explicitly.
- Generate custom RSS feeds as the recommendation delivery channel: not built — no
  "recommended for you" feature exists yet to generate a feed *for*. Revisit once M2's
  discovery signals (or M4's scoring) actually produce a ranked list worth delivering this
  way, rather than building the delivery mechanism first.
- **hark as a gpodder-sync server, done (0.12.0):** `gpodder_server.py` implements the exact
  four endpoints AntennaPod's own `NextcloudSyncService.java` calls — confirmed against
  AntennaPod's actual source (github.com/AntennaPod/AntennaPod) rather than guessed from
  the server side. Its `login()`/`logout()` are no-ops (no Nextcloud handshake/capabilities
  probe to fake), so **AntennaPod's existing "Nextcloud" sync setting can point at hark
  directly, with zero app changes** — this is the "non-invasive" path discussed for a
  hypothetical AntennaPod fork: the sync half doesn't need a fork at all, just protocol
  compatibility on hark's side. Auth is HTTP Basic against the same account the web UI
  uses (`Auth.verify`) — no second credential to manage. A new `subscription_changes`
  table gives hark its own timestamped add/remove history (`shows` only holds current
  state), so a repeat sync stays incremental via `since=`. `listen_actions` gained a
  `started` column (protocol requires it for a valid PLAY action) that the original
  Nextcloud-*client* path (`cmd_sync_history`) had been silently dropping — both directions
  now go through one `gpodder_server.record_episode_actions()`.
  - Still open (a fork, not just protocol compat, would need real Android/Kotlin work —
    a much heavier commitment than anything else in this project): transparent
    ad-stripped playback (AntennaPod asking hark for a cut audio URL before falling back
    to the original) and surfacing hark's discovery/notable-episodes in-app. Not started;
    revisit only if the sync-server approach alone doesn't get the desired experience.

## Deployed pipeline automation (2026-07-12)

The `transcribe` service's compose command now runs the *entire* free pipeline
unattended, not just transcription:
- **Once at container start:** `sync-subscriptions`, `sync-history`.
- **Every ~60-90s cycle:** `fsck --fix`, load any dropped-in comparisons/extractions/
  ad-detections batch, `transcribe --cross-show-only --limit 5`, `cut`.
- **Every ~30 minutes** (gated by a `.last_slow_cycle` marker file's mtime, not every
  cycle — 73+ shows' RSS feeds don't need refetching every 60-90s): `ingest`, `canon`,
  `chapters`.

A 2026-07-12 gpodder sync brought in 67 shows' full back catalogs in one shot
(~2,200 → ~24,000 total episodes), which is why `ingest` alone now matters enough to
automate — most of that corpus was previously invisible to hark entirely.

**Extraction, claims comparison, and ad-span detection all stay session-as-X** (no
`$ANTHROPIC_API_KEY` anywhere in this project, deliberately — see M1's history above)
but are no longer manual-only: `claude-fleet`'s `jobs/agents/hark-pipeline.md` is a
scheduled unattended agent (hourly, `systemd/fleet-hark-pipeline.timer`, `Host=code` —
the earlier install had it sitting on the fleet primary with nothing enabled to run it,
so it silently never fired; see below) that reads hark's production db directly
(same read-only shared-mount access this session used manually), does the
extraction/comparison/detection judgment itself as a Claude agent, and drops the
output as `pending-extractions.jsonl`/`pending-comparisons.jsonl`/
`pending-ad-detections.jsonl` for the deployed `transcribe` service to pick up and
load — same mechanism, just scheduled instead of ad hoc. Batch sizes (40 episodes in
sub-batches of ~15 for extraction, 3 topics for comparison, ~10 episodes for
ad-detection) and the hourly cadence are sized for the post-sync backlog; revisit once
it's actually cleared. Two real bugs found running this the first time, both fixed in
the job file: `claude -p`'s headless sandbox blocks `cd`/`ls` outside the launch
directory (use `uv run --project`/`test -f` instead, matching `board-minutes.md`'s own
absolute-path style), and a single very large tool result (querying 150 episodes in
one shot) correlated with the job dying mid-run — smaller sub-batches avoid it.

**Ad-span detection (0.13.0)** closes the gap where production ad-stripping was
silently chapter-markers-only: `detect-ads` (the LLM-over-transcript classifier —
the whole reason adscrub exists, since chapter markers alone miss host-read/
dynamically-inserted ads) had never been added to the fast loop, since it needs
`$ANTHROPIC_API_KEY` like `extract`/`compare` did — but unlike those two, it never
got the session-as-X treatment either, so it just never ran at all. Meanwhile
`transcribe --cross-show-only` *was* running automatically, so transcripts piled up
in `llm_detected_at IS NULL` limbo indefinitely. Fixed the same way extraction/compare
were: a third `hark-pipeline.md` section reads each pending episode's transcript
(the same numbered/timestamped segment format `ClaudeAdDetector`'s own prompt uses)
and picks ad-span segment indices, `hark load-ad-detections` stores them via
`adscrub.detect.detect_episode()`/`spans_from_segment_indices()` (adscrub 0.5.0)
unchanged. **Not a genuinely real-time ("on the fly") path** — a fresh episode needs
transcription *and* LLM classification before any span exists, both too slow to do
synchronously at audio-serve time — so this stays a pre-computed batch step folded
into the same hourly cadence as extraction/comparison rather than a separate tighter
timer (no case yet for that added complexity). End-to-end lag: up to an hour for
detection to run, then within the next ~60-90s fast-loop tick for `cut` to act on it —
the best available without paying for a live API.

**Verifying "automatic" actually means unattended, not just present:** the timer can
be installed and enabled while never having actually fired — `systemctl --user
list-timers` showing a near-future trigger only proves it's *scheduled*, not that a
real run has completed successfully end to end. `systemctl --user start
fleet-agent@hark-pipeline.service` runs exactly what the timer would, on demand;
check `/mnt/Tap/apps/hark/data/loaded-*-<timestamp>.jsonl` mtimes afterward (matched
against the drop-in file's own initial `pending-*` write, which the fast loop
consumes within ~90s) to confirm the whole chain — agent run → drop-in file → loaded
by the deployed pipeline — actually completed, not just that pieces of it exist.

**Not every synced show is worth extracting from — 0.11.0's `topic_index_enabled`
scopes this.** The 67 shows the 2026-07-12 gpodder sync added are mostly not
subject-per-episode genre shows at all (news, politics, personal finance) — extraction
on one of those just burns session-as-X effort for a guaranteed-empty result. New shows
now default to excluded from extraction until reviewed from the show page; hand-curated
`hark resolve` shows default included, matching prior behavior. `hark discover`'s
existing genre-filtered search is the fast path for finding shows that likely *should*
be enabled without reviewing all 67 by hand.

## Multi-user accounts (done, 0.14.0)

Not in the original milestone list — added on explicit request, after the gpodder-sync
server (M3) made it obvious more than one person could point AntennaPod at hark.

- **The core design call: what stays global vs. what becomes per-user.** `shows`,
  `episodes`, transcripts, `ad_segments` all stay global and shared across every
  account — a show two people both subscribe to still gets transcribed/ad-detected
  exactly once, not once per subscriber. Only what's inherently *personal* moved to a
  per-account scope: the subscription list itself (new `user_shows` table — current
  state, same relationship to `subscription_changes`' event log that `shows` has to
  `episodes`) and listen history (`listen_actions` gained `user_id`, including in its
  own UNIQUE constraint — two accounts playing the same episode at the same timestamp
  must not collide the way two AntennaPod installs on *one* account correctly do).
- **No FK from user_shows/subscription_changes/listen_actions to `users`.** `users`
  lives in the separate auth.db (see web.py's module docstring for why: sessions must
  survive a hark.db data-snapshot restore) — `user_id` here is a soft cross-database
  reference by convention, same category as `feed_token`/the ad-stripping toggles
  already being hark.db-resident config that doesn't travel with a snapshot swap.
- **gpodder-sync is now genuinely multi-tenant.** `gpodder_server.py`'s four functions
  and `web.py`'s four HTTP handlers thread the Basic-Auth-resolved user_id through
  instead of operating on the global tables unscoped — each account's AntennaPod syncs
  against its own subscription list and listen history, invisibly to every other
  account. `record_subscription_changes` still calls `resolve.add_show_by_feed_url`
  (global catalog, unchanged) and layers a `user_shows` upsert/delete on top.
- **`is_admin` (auth.db), added alongside multi-user rather than deferred**: without
  it, any new account could flip the global ad-stripping/topic-index toggles for a
  show every other account also depends on — those are genuinely shared settings, not
  personal preference, so they're gated to admin accounts (403 on the route, hidden in
  the UI) once more than one account can exist. The bootstrap account becomes admin
  automatically; `hark user add --admin` grants it going forward.
- **User management is CLI-only for now** (`hark user add/list/remove`, operating on
  `--auth-db`) — a web admin page is a plausible fast-follow but wasn't asked for, and
  shell access is already the trust boundary every other admin-only action in this
  project assumes (same category as running `hark resolve`/`discover --add` directly).
  A new account has no password yet; it logs in once with `$HARK_ADMIN_TOKEN` (same
  shared bootstrap token, works for any passwordless row, not just literally "admin")
  and sets a real password at `/account` — no new bootstrap mechanism needed.
- **`/shows` defaults to "my list"** (`?all=1` browses the full catalog) and the show
  page gained subscribe/unsubscribe — the web-UI equivalent of what AntennaPod's own
  gpodder sync already does, for browsing/subscribing without a podcast app open.
  Dashboard/topic index/claims comparison stay global and unfiltered — those are about
  real-world content, not personal curation, so every account sees the same one.
- **Found and fixed along the way:** `auth.db` never actually turned on `PRAGMA
  foreign_keys` (it's a per-connection setting, not a schema property, and was never
  set on the connections `Auth`'s own methods open) — so `sessions.user_id`'s `ON
  DELETE CASCADE` had silently never been enforced. `hark user remove` needing that
  cascade to actually clean up a deleted account's sessions is what surfaced it.
- **Migration for existing single-account databases:** `user_shows` backfills the
  bootstrap account (auth.db id 1, always the first row `Auth.__init__` inserts) with
  every show that already existed before the table did, so upgrading doesn't blank out
  the one real account's dashboard. `subscription_changes`/`listen_actions` rows from
  before `user_id` existed attribute to that same id 1. `listen_actions` needed a full
  table rebuild (rename/create/copy/drop), not a plain `ALTER TABLE ADD COLUMN` — a new
  UNIQUE constraint can't be bolted onto an existing table any other way in SQLite.

## Invite links + per-user quota (done, 0.15.0)

Follow-up to multi-user, on explicit request: 0.14.0's account creation only had the
shared `$HARK_ADMIN_TOKEN` bootstrap, workable for the owner's own account but not
something you'd want to hand to a friend (it's a master credential, not scoped to
their account) — and 0.14.0 shipped with no cap on how many shows a non-admin account
could add, "make sure users can't add too many podcasts" being an explicit ask here.

- **Invite links, not a bigger shared secret.** `Auth.create_invite()` generates a
  single-use `invite_token` (new `users` column, `auth.db`) scoped to exactly one
  account, expiring after `INVITE_EXPIRES_DAYS` (7). `/invite/<token>` (unauthenticated,
  token-gated — same category as `/feed`/`/audio`'s own token gating) lets that person
  set their password directly and logs them in; `accept_invite()` clears the token
  (single-use) in the same call that sets it. `hark user add`'s original bootstrap-token
  flow stays available unchanged — `create_invite`/`create_user` are siblings, not a
  replacement.
- **`/admin/users` exists because CLI-only user management wasn't actually usable
  day to day** — this project's own homelab deploy has no container shell access (see
  the earlier "still-open" note above this section, now moot for this specific need),
  so an admin without shell access literally couldn't run `hark user add`/`invite`
  before this. The page reuses the same `Auth` methods the CLI does; neither is more
  authoritative than the other.
- **Invite links persist, not just the one post-creation redirect.** First cut only
  showed the link in the redirect's query string — reasonable-looking until you
  actually lose the tab before copying it, with no way to see it again short of
  deleting and recreating the invite. `list_users()` now returns the raw `invite_token`
  (not just an `invite_pending` boolean) so both `/admin/users` and `hark user list`
  can always re-show a still-pending invite's link.
- **`MAX_SHOWS_PER_USER = 10`, admin-exempt, enforced identically on both paths that
  can add a subscription** — `web.py`'s `subscribe()` (web UI) and
  `gpodder_server.record_subscription_changes()` (AntennaPod sync) both check it, one
  constant defined in `gpodder_server.py` and imported by `web.py` rather than defined
  twice. The web path can show a real error (`?err=quota` → a message on the show
  page); the AntennaPod-sync path can't — gpodder-sync has no per-item
  rejection/partial-success signal, so a feed_url that would push a non-admin over the
  cap is dropped silently (no `subscription_changes` row, no `user_shows` row) rather
  than logged and then re-offered as "added" on the next `since=` sync.
- **Found and fixed along the way:** `Auth.set_password()` ran a bare `DELETE FROM
  sessions` with no `WHERE user_id` — harmless before multi-user (only one account's
  sessions ever existed to delete), but unscoped it would silently log out *every*
  account the instant any single one of them changed a password. Surfaced by invite
  acceptance, which calls `set_password()` immediately before creating the new
  account's own first session.

## Codebase audit + admin-editable base URL (done, 0.16.0)

A systematic file-by-file audit pass ("audit to zero then refactor to zero then audit
to zero again," on explicit request) found and fixed six real bugs, none caught by the
existing test suite until this pass added regression tests for each:

- `ingest_show()` only wrapped the HTTP fetch step in its own error isolation —
  `parse_feed()`/`upsert_episodes()` running outside that try/except meant one show's
  parse/upsert failure could propagate uncaught out of `ingest_all()`'s list
  comprehension, aborting every show still queued after it. Worse, since `ingest_all()`
  reuses one connection across every show, a partial write left uncommitted here would
  still be pending when some *later* show's own `conn.commit()` ran, silently
  persisting a broken write alongside an unrelated show's good one. Fixed by wrapping
  the whole parse+upsert+update block with an explicit `conn.rollback()` on failure.
- `pipeline.upsert_topic()` could silently merge two distinct Wikidata entities that
  happen to share a display label (`Canonicalizer` just returns the raw
  `wbsearchentities` hit label, no disambiguation — e.g. "Mercury" the planet vs. the
  element). `recanonicalize()` already had a guard against exactly this collision;
  `upsert_topic()` — the normal extraction hot path, hit on every real run, not just
  the offline recovery pass — didn't, so a topic could get attributed to the wrong
  entity with its own real QID silently discarded. Now disambiguates the label
  (matching `recanonicalize()`'s own `(QID)` suffix convention) instead of merging.
- A quota-bypass race: `subscribe()` (web UI) and `record_subscription_changes()`
  (AntennaPod sync) each read the current per-user show count, then conditionally
  inserted, as separate statements with no lock held between them. The web server is
  threaded (`ThreadingHTTPServer`) and gpodder-sync opens a fresh connection per
  request, so two concurrent calls for the same user near the cap — a double-click, two
  tabs, or the same account syncing from two AntennaPod installs at once — could each
  read a count under `MAX_SHOWS_PER_USER` before either committed, letting both through
  and landing above it. Fixed with `BEGIN IMMEDIATE` on both, taking the write lock up
  front so a second concurrent call blocks until the first resolves; verified against
  the actual race with two real threads and a barrier (fails reliably without the fix,
  passes reliably with it — not just a plausible-sounding theory).
- The same falsy-zero bug (`if limit:` treating `limit=0` as "no limit" instead of
  "zero results") independently in three places: `claims.pending_topics()`,
  `cli._filter_enabled()`, and `queries.topics_query()` — the third one found during
  the refactor pass specifically *because* the first two made the pattern recognizable,
  underscoring the value of the "audit again" pass rather than stopping after one.
  Reachable via `hark compare/transcribe/detect-ads/cut/topics --limit 0`.

The refactor-to-zero pass itself found no further simplification worth making — the
codebase, group by group, was already at a reasonable clarity/duplication equilibrium
(the web.py module split earlier in 0.15.0 already having been the refactor this
project actually needed). A couple of tempting-looking "unify this repeated retry-loop
shape" candidates in `cli.py` turned out to have deliberately different exception
handling per call site, confirmed against `adscrub`'s own upstream CLI precedent, so
left alone rather than flattened into a leakier abstraction.

**Admin-editable base URL**, a direct follow-up: `--base-url`/`$HARK_BASE_URL` was
previously the *only* way to set the public URL invite links and the podcast
feed/audio routes get built from — a redeploy just to fix a wrong hostname. Now
`/admin/users` has a "Server settings" section to set (or reset) an override live,
stored in `auth.db`'s new generic `settings` key/value table — deliberately not
`hark.db`, for the same reason accounts/sessions live there: a pipeline-pushed data
snapshot replaces `hark.db` wholesale and must never wipe server config any more than
it should wipe accounts. `App.base_url` became a property that re-reads the override on
every access (not cached) so an edit from one request is visible on the very next one,
no restart needed. `hark user invite` (CLI) checks the same override; an explicit
`--base-url` flag still wins outright over it.

## M4 — episode scoring (tiltmeter-style)

- Defined interestingness metrics, calibration loop against owner ratings.
- Per-topic treatment comparison (depth, sensationalism) — needs transcripts for
  fidelity. Whisper is already available via the adscrub dependency (see above);
  this milestone should call `adscrub.transcribe`'s functions the same way the
  ad-stripping pipeline does, not stand up its own transcription path.

## Seed shows (feeds.txt)

Start with well-known subject-per-episode shows across two genres, e.g.: Casefile,
Casual Criminalist, Swindled, The Rest Is History, Short History Of, Cautionary Tales.
Resolve their real feed URLs via iTunes Search API at runtime — do not hand-copy URLs.

## Open questions (owner input needed, don't block on these)

- ~~Hosting~~ Resolved 2026-07-10: private Gitea (`flan/hark`); revisit GitHub if it goes public.
- Which LLM/provider for extraction (M1 decision).
- ~~GPU/Whisper feasibility~~ Resolved 2026-07-11: `code` has a real GPU, Docker's
  `nvidia` runtime is registered — see the adscrub dependency section above.
- ~~How to make the adscrub path dependency work in the Docker build~~ Resolved
  2026-07-11: `scripts/build-image.sh`, see the ad-stripping section above.
- `hark detect-ads` (the live-API path, still available for manual/local use) defaults
  to `claude-opus-4-8`; revisit accuracy on ad-span boundaries — the deployed instance
  uses the session-as-X `load-ad-detections` path instead (0.13.0), so this mainly
  matters if the live path ever gets used for real.
- ~~When to actually wire the gpodder/Nextcloud subscription sync~~ Resolved 2026-07-12:
  M3 shipped in 0.10.0 — see above.
