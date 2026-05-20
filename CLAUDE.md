# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Matrix bot (`matrix-nio` + E2EE) that listens for custom room events from a companion music bot and scrobbles tracks to Last.fm on behalf of linked users. It runs as a standalone async Python process.

## Running

```bash
# Install dependencies (Python 3.11+ recommended; 3.10 works with tomli fallback)
pip install -r requirements.txt

# Copy and fill in config
cp config/config.toml.example config/config.toml

# Generate a Fernet encryption key for the storage section
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Run
python main.py
```

Logs go to `logs/scrobbler.log` (rotating, 10 MB × 5). The `data/` directory holds the SQLite DB, device ID, crypto store, and access token — all auto-created on first run.

## Architecture

### Event flow (the core loop)

The companion music bot emits two custom Matrix room events:
- `dev.elementcall.musicbot.track_started` — fired when a track begins playing; carries artist/track/album/duration, a UUID `play_id`, and the list of current call participants
- `dev.elementcall.musicbot.track_finished` — fired when a track ends; carries `play_id`, `played_s`, `reason` (`"finished"` | `"skipped"` | `"stopped"` | `"error"`), and `eligible_participants` (users present for both start and finish)

`EventHandler` (`event_handler.py`) dispatches these to `PlayTracker` (`play_tracker.py`).

### PlayTracker state machine

`on_track_started`: inserts a `play_state` row for each linked, non-blacklisted participant; fires a `now_playing` update to Last.fm immediately.

`on_track_finished`: reads `play_state` rows for that `play_id`, checks the Last.fm threshold (`played_s >= 30` and `played_s >= duration/2` or `>= 240`), and enqueues a `scrobble_queue` row for eligible users. Deletes `play_state` rows when done.

Two background asyncio tasks run continuously:
- **drain loop** (`_drain_once`): polls `scrobble_queue` on a timer, submits to Last.fm, retries with exponential backoff (`BACKOFF = [60, 300, 1800, 7200, 21600]`), drops after 5 failures
- **sweep loop**: deletes `play_state` rows that are older than `2 * duration_s + grace_seconds` (catches tracks the bot missed a `track_finished` for)

### Storage (`storage.py`)

Single `aiosqlite` connection. Last.fm session keys are encrypted at rest with Fernet. Schema tables:
- `linked_accounts` — Matrix user ↔ Last.fm session key
- `pending_links` — in-flight OAuth tokens (between `!fm link` and `!fm confirm`)
- `play_state` — in-flight plays (cleared on `track_finished` or sweep)
- `scrobble_queue` — durable retry queue
- `recently_scrobbled` — last scrobble per user, used by `!fm love`
- `room_blacklist` — per-user room opt-outs

### Scrobbler interface (`scrobblers/base.py`)

`Scrobbler` is a `Protocol` with `now_playing`, `scrobble`, `love`, `start_linking`, `finalize_linking`. Only `LastFmScrobbler` is implemented; the protocol is designed to accommodate ListenBrainz later.

### Bot startup sequence (`bot.py`)

1. Open storage → setup `AsyncClient` with E2EE store → login (token restore or password) → upload/query keys
2. Register callbacks: `AutoAccept` (`auto_accept.py`) handles room invites from the `auto_accept_room_invites_from` allowlist; `_register_verification_callbacks` handles SAS verification from the `auto_accept_users` allowlist — these are separate concerns with separate config keys.
3. Initial `sync(full_state=True)` → replay recent timeline (recover missed `track_finished` events from the last `recovery_lookback_hours`)
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

**Why `/verify` alone is not enough:** Without cross-signing bootstrap, the device shows as "Verification successful" in the dialog but remains "Unverified" in the session list. The bot must upload its own master/self-signing/user-signing keys on startup (via `cross_signing.py:ensure_cross_signing()`) for Element to complete the full trust chain. This is already wired into `_setup_e2ee` in `bot.py`.

## Filtering (`play_tracker.py:_should_accept_track`)

Before inserting a `play_state` row, incoming `track_started` events are filtered by:
- `[filter.emitters] allowed_user_ids` — allowlist of Matrix user IDs allowed to emit events (`["*"]` = all)
- `[filter.sources] allowed` / `denied` — allowlist/denylist of source strings (`"youtube"`, `"youtube_music"`, `"other"`)
- `[filter.quality] require_high` — if true, only accept events where `metadata_quality == "high"` (artist/track from YouTube metadata tags, not title-parsed)
- Empty artist or track always drops the event

## Key design constraints

- `share_group_session` is called before every `room_send` with `ignore_unverified_devices=True`, matching the musicbot approach — messages are encrypted but device verification is not enforced.
- The `play_id` is the unit of idempotency. `INSERT OR IGNORE` on `play_state` and checking for existing rows in `on_track_finished` make duplicate events safe.
- `call_participants` in `track_started` must be non-empty for any scrobbles to happen — if the companion musicbot is restarted while a call is in progress, it must re-seed its participant state before emitting events.
