# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Matrix bot (`matrix-nio` + E2EE) that listens for custom room events from a companion music bot and scrobbles tracks to Last.fm on behalf of linked users. It runs as a standalone async Python process.

## Running

```bash
# ../matrix-element-call-common must be cloned as a sibling directory first
# Install dependencies (Python 3.11+)
pip install -r requirements.txt

# One-time setup: login, IDs, Last.fm and storage keys, cross-signing keys (config is optional)
python scripts/setup.py

# Run
python main.py
```

**Docker:** `Dockerfile` + `docker-compose.yml` mirror the music bot's: `python:3.14-slim`, deps in `/opt/venv`, `matrix-bot-common` from the GitHub tarball (`COMMON_URL` build arg; the `-e ../` line of `requirements.txt` is filtered out), uid 10001, volumes for `/app/data` and `/app/logs`, `./config` mounted read-only. CI (`.github/workflows/ci.yml`) compiles/imports the modules and publishes `ghcr.io/oolaapleur/matrix-element-call-scrobbler` (`latest`, last tag, sha; amd64+arm64) on every push to `master`. `main.py` cancels the bot on SIGTERM/SIGINT and `ScrobblerBot.run` closes storage in a `finally`: as PID 1 SIGTERM has no default action, and an open aiosqlite connection's non-daemon thread kept the process alive after a cancel during start-up.

Logs go to `logs/scrobbler.log` (INFO and up — `aiosqlite`/`peewee`/`nio` DEBUG used to fill 10 MB every two days; rotating, 10 MB × 5). The `data/` directory holds the SQLite DB, device ID, crypto store, and access token — all auto-created on first run.

## Architecture

### Event flow (the core loop)

The companion music bot emits two custom Matrix room events:
- `dev.elementcall.musicbot.track_started` — fired when a track begins playing; carries artist/track/album/duration, a UUID `play_id`, and the list of current call participants
- `dev.elementcall.musicbot.track_finished` — fired when a track ends; carries `play_id`, `played_s`, `reason` (`"finished"` | `"skipped"` | `"stopped"` | `"error"`), and `eligible_participants` (users present for both start and finish)

`EventHandler` (`event_handler.py`) dispatches these to `PlayTracker` (`play_tracker.py`).

### PlayTracker state machine

`on_track_started`: inserts a `play_state` row for each linked, non-blacklisted participant; fires a `now_playing` update to Last.fm immediately (skipped for events replayed after a restart whose track is already over).

`on_track_finished`: ignores play_ids already in `finished_plays`; reads `play_state` rows for that `play_id` (same room), checks the Last.fm threshold (`played_s >= 30` and `played_s >= duration/2` or `>= 240`), and enqueues a `scrobble_queue` row for eligible users. Queueing, deleting the `play_state` rows and recording the play_id in `finished_plays` happen in one transaction (`Storage.finish_play`), so replays can't scrobble twice.

Two background asyncio tasks run continuously:
- **drain loop** (`_drain_once`): polls `scrobble_queue` on a timer, submits to Last.fm, retries with exponential backoff (`BACKOFF = [60, 300, 1800, 7200, 21600]`), drops after 5 failures. Errors are classified by the Last.fm error code in the JSON body (`LastFmError`, `scrobblers/lastfm.py`), not by the HTTP status: code 9 (invalid session) unlinks the user (disabling let `!fm on` re-enable the dead key) and posts a notice to the room stored in `scrobble_queue.room_id`; codes 6/7 (invalid parameters) and an ignored scrobble (`ScrobbleIgnored`, except ignoredMessage code 5 = daily limit, which is retried) drop the row; 29 (rate limit) also ends the drain pass; everything else (outages, rate limit, bad/suspended API key, network) is retried. Before 2026-11-25 any "403" in the error text disabled the user — which an API-key problem would have done to everyone — and the notice never went out (the queue had no `room_id` column; `Storage.open()` adds it to old databases).
- **sweep loop**: deletes `play_state` rows that are older than `2 * duration_s + grace_seconds` (catches tracks the bot missed a `track_finished` for); a play with unknown duration (0) counts as `UNKNOWN_DURATION_S` = 1 h, otherwise it was swept within minutes

### Storage (`storage.py`)

Single `aiosqlite` connection. Last.fm session keys are encrypted at rest with Fernet. Schema tables:
- `linked_accounts` — Matrix user ↔ Last.fm session key
- `pending_links` — in-flight OAuth tokens (between `!fm link` and `!fm confirm`)
- `play_state` — in-flight plays (cleared on `track_finished` or sweep)
- `scrobble_queue` — durable retry queue
- `recently_scrobbled` — last scrobble per user, used by `!fm love`
- `room_blacklist` — per-user room opt-outs
- `finished_plays` — play_ids whose `track_finished` was handled (replay idempotency), pruned after `recovery_lookback_hours`

### Scrobbler interface (`scrobblers/base.py`)

`Scrobbler` is a `Protocol` with `now_playing`, `scrobble`, `love`, `start_linking`, `finalize_linking`. Only `LastFmScrobbler` is implemented; the protocol is designed to accommodate ListenBrainz later.

### Bot startup sequence (`bot.py`)

1. Open storage → setup `AsyncClient` with E2EE store → login (token restore or password) → upload/query keys
   - Setup: `scripts/setup.py` (one run, idempotent) logs in once (token → `data/access_token`, device → `data/device_id`; password not stored) and writes `data/credentials.json` (homeserver, user_id, owners, music_bots, lastfm_api_key/shared_secret, encryption_key; 0600), then restores cross-signing keys via `matrix_bot_common.restore_keys`. `Config` layers the optional `config/config.toml` over it (config wins); `[matrix] password` is only used when there is no saved token
2. Register callbacks: `AutoAccept` (`auto_accept.py`) handles room invites from the `auto_accept_room_invites_from` allowlist; `_register_verification_callbacks` handles SAS verification from the `auto_accept_users` allowlist — these are separate concerns with separate config keys. `auto_accept_users` (default: setup's owners) is a legacy path from before the bot signed its own device; deliberately left out of the README, setup prompt and example config since 2026-11-27 (users verify the bot by emoji from the bot's own Element session).
3. Initial `sync(full_state=True)` → replay recent timeline: up to 500 recent events per room (`room_messages`, decrypted by nio) **within `recovery_lookback_hours`** (by `server_timestamp`), oldest first, both `track_started` and `track_finished` through the normal `EventHandler` — the first sync after a long downtime is "limited" and drops older events. `finished_plays` makes it idempotent.

**Event callbacks must never raise**: nio stores the sync token before running callbacks and doesn't catch their exceptions, so a failing callback silently loses the rest of that sync batch. `_on_room_message`/`_on_unknown_event` catch everything; a failed `!fm` command answers with the error.
4. Start background tasks → enter `sync_forever` loop with exponential backoff on errors

### User-facing commands

All commands begin with `!fm` and are handled by `CommandRouter` → `LinkingManager`. The linking flow is a standard Last.fm OAuth token dance: `!fm link` gets a token and returns the auth URL; `!fm confirm` exchanges the token for a session key.

## Device Verification

After first run (or after wiping `data/crypto_store/`), verify the bot's device in Element using the `/verify` slash command:

```
/verify <device_id> <ed25519_fingerprint>
```

Get the values:
```bash
# device_id
cat data/device_id

# ed25519 fingerprint
python3 -c "
from nio.store import SqliteStore
did = open('data/device_id').read().strip()
store = SqliteStore('@yourbotname:matrix.org', did, 'data/crypto_store/')
acc = store.load_account()
print(acc.identity_keys['ed25519'])
"
```

Run `/verify <device_id> <fingerprint>` in any Element room as `@youruser:matrix.org`. The bot auto-accepts the cross-signing.

**Why `/verify` alone is not enough:** Without cross-signing bootstrap, the device shows as "Verification successful" in the dialog but remains "Unverified" in the session list. The bot must upload its own master/self-signing/user-signing keys on startup (via `matrix_bot_common.cross_signing:ensure_cross_signing()`) for Element to complete the full trust chain. This is already wired into `_setup_e2ee` in `bot.py`.

## Filtering (`play_tracker.py:_should_accept_track`)

Before inserting a `play_state` row, incoming `track_started` events are filtered by:
- `[emitters] allowed_user_ids` — allowlist of Matrix user IDs allowed to emit events, checked against `event.sender` for both `track_started` and `track_finished` (`["*"]` = all). Until 2026-11-25 it compared `content.emitter.user_id`, which the sender writes itself, so any room member could fake plays; set it to the music bot's user ID (this deployment's value is in `CLAUDE.local.md`, not committed)
- `[sources] allowed` / `denied` — allowlist/denylist of source strings (`"youtube"`, `"youtube_music"`, `"other"`)
- `[quality] require_high` — if true, only accept events where `metadata_quality == "high"` (artist/track from YouTube metadata tags, not title-parsed)
- Empty artist or track always drops the event

## Key design constraints

- `share_group_session` is called before every `room_send` with `ignore_unverified_devices=True`, matching the musicbot approach — messages are encrypted but device verification is not enforced.
- Replies are written in a small Markdown subset (`**bold**`, `` `code` ``, newlines) and sent with an HTML `formatted_body` built by `bot.markdown_to_html`; `body` keeps the Markdown as the plain-text fallback.
- The `play_id` is the unit of idempotency. `INSERT OR IGNORE` on `play_state` and checking for existing rows in `on_track_finished` make duplicate events safe.
- `call_participants` in `track_started` must be non-empty for any scrobbles to happen — if the companion musicbot is restarted while a call is in progress, it must re-seed its participant state before emitting events.
