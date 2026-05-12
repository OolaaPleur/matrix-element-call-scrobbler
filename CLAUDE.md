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
- `dev.oolaa.musicbot.track_started` — fired when a track begins playing; carries artist/track/album/duration, a UUID `play_id`, and the list of current call participants
- `dev.oolaa.musicbot.track_finished` — fired when a track ends; carries `play_id`, `played_s`, `reason` (`"finished"` | `"skipped"` | `"stopped"` | `"error"`), and `eligible_participants` (users present for both start and finish)

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
2. Initial `sync(full_state=True)` → replay recent timeline (recover missed `track_finished` events from the last `recovery_lookback_hours`)
3. Start background tasks → enter `sync_forever` loop with exponential backoff on errors

### User-facing commands

All commands begin with `!fm` and are handled by `CommandRouter` → `LinkingManager`. The linking flow is a standard Last.fm OAuth token dance: `!fm link` gets a token and returns the auth URL; `!fm confirm` exchanges the token for a session key.

## Key design constraints

- `ignore_unverified_devices=False` is intentional — the bot refuses to send to unverified devices to preserve E2EE guarantees. All participants must have verified devices.
- The `play_id` is the unit of idempotency. `INSERT OR IGNORE` on `play_state` and checking for existing rows in `on_track_finished` make duplicate events safe.
- `MUSICBOT_CHANGES.md` documents what changes are needed in the **companion music bot** to emit the custom events this bot consumes.
