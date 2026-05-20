# Matrix Element Call Scrobbler

A Matrix bot that listens for playback events from [matrix-element-call-musicbot](https://github.com/OolaaPleur/matrix-element-call-musicbot) and scrobbles tracks to Last.fm on behalf of linked users.

Multiple users in the same Element Call session each get their own scrobbles to their own Last.fm account — the bot handles all of them in parallel.

## How It Works

The scrobbler pairs with [matrix-element-call-musicbot](https://github.com/OolaaPleur/matrix-element-call-musicbot). When the music bot starts playing a track in an Element Call session, it emits custom Matrix room events carrying the track metadata and the list of active call participants. The scrobbler:

1. Immediately updates "now playing" on Last.fm for every linked participant
2. Scrobbles the track when it finishes, subject to Last.fm's eligibility rules (≥30 s played and ≥50% of duration, or ≥240 s)
3. Retries failed scrobbles with exponential backoff (up to 5 attempts over several hours)
4. Persists the retry queue in SQLite so scrobbles survive bot restarts

## Requirements

- Python 3.11+ (3.10 works with the bundled `tomli` fallback)
- A Matrix account for the bot (any homeserver)
- A [Last.fm API account](https://www.last.fm/api/account/create) (free) — you need an API key and shared secret
- [matrix-element-call-musicbot](https://github.com/OolaaPleur/matrix-element-call-musicbot) running in the same room(s)

## Setup

### 1. Clone and install

```bash
# Clone this repo and the shared library as siblings
git clone https://github.com/OolaaPleur/matrix-element-call-scrobbler
git clone https://github.com/OolaaPleur/matrix-element-call-common
cd matrix-element-call-scrobbler
pip install -r requirements.txt   # resolves ../matrix-element-call-common automatically
```

> [!NOTE]
> `requirements.txt` references `../matrix-element-call-common` (the [shared library](https://github.com/OolaaPleur/matrix-element-call-common)). Both repos must be cloned into the same parent directory.

### 2. Configure

```bash
cp config/config.toml.example config/config.toml
```

Edit `config/config.toml` and fill in:

**`[matrix]`**
- `homeserver` — your Matrix homeserver URL (e.g. `https://matrix.org`)
- `user_id` — the bot's full Matrix user ID (e.g. `@my-scrobbler:matrix.org`)
- `password` — the bot account password

**`[matrix.verification]`**
- `auto_accept_users` — your own Matrix user ID; the bot will auto-accept cross-signing verification requests from these users

**`[lastfm]`**
- `api_key` and `shared_secret` — from your [Last.fm API account](https://www.last.fm/api/account/create)

**`[storage]`**
- `encryption_key` — generate a Fernet key for encrypting Last.fm session tokens at rest:
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```

**`[behavior]`**
- `auto_accept_room_invites_from` — list of Matrix user IDs allowed to invite the bot into rooms (typically just yourself)

See the full [`config/config.toml.example`](config/config.toml.example) for all options with comments.

### 3. Run

```bash
python main.py
```

Logs go to `logs/scrobbler.log` (rotating, 10 MB × 5 files). The `data/` directory (SQLite DB, device ID, crypto store, access token) is created automatically on first run.

## Who Can Use the Bot?

The bot owner controls which rooms the bot joins (via `auto_accept_room_invites_from`). Once the bot is in a room, **any Matrix user** in that room can link their own Last.fm account and receive scrobbles — they just need to participate in Element Call sessions while the music bot is playing.

Users link their accounts themselves via bot commands; they never share credentials with the bot owner.

## User Commands

All commands are sent as regular Matrix messages in the room (prefix `!fm`):

| Command | Description |
|---------|-------------|
| `!fm link` | Start the Last.fm account linking flow — returns an auth URL to open in your browser |
| `!fm confirm` | Complete linking after authorizing at the Last.fm URL |
| `!fm unlink` | Unlink your Last.fm account from this bot |
| `!fm status` | Show whether your Last.fm account is linked |
| `!fm love` | Love the most recently scrobbled track on Last.fm |
| `!fm blacklist` | Opt out of scrobbling for the current room |
| `!fm whitelist` | Re-enable scrobbling for the current room |

## Device Verification

After first run (or after wiping `data/crypto_store/`), verify the bot's Matrix device to establish full E2EE trust. Get the values to verify with:

```bash
# Device ID
cat data/device_id

# Ed25519 fingerprint
python3 -c "
from nio.store import SqliteStore
did = open('data/device_id').read().strip()
user_id = 'YOUR_BOT_USER_ID'   # e.g. @my-scrobbler:matrix.org
store = SqliteStore(user_id, did, 'data/crypto_store/')
acc = store.load_account()
print(acc.identity_keys['ed25519'])
"
```

Then in Element, run `/verify <device_id> <fingerprint>` from your own account. The bot auto-accepts the cross-signing confirmation.

## Filtering Playback Events

The bot can be configured to only scrobble certain kinds of tracks. All filters are in `config/config.toml`:

| Section | Key | Effect |
|---------|-----|--------|
| `[filter.emitters]` | `allowed_user_ids` | Allowlist of music bot Matrix IDs (`["*"]` = any) |
| `[filter.sources]` | `allowed` / `denied` | Allowlist/denylist of source strings (`"youtube_music"`, `"youtube"`, `"other"`) |
| `[filter.quality]` | `require_high` | If `true`, only scrobble when the music bot provided artist/track from file tags (not title-parsed heuristics) |

Events with an empty artist or track name are always dropped.

## Architecture

| File | Purpose |
|------|---------|
| `bot.py` | Startup, E2EE setup, sync loop with exponential backoff |
| `event_handler.py` | Dispatches Matrix events to the tracker |
| `play_tracker.py` | Core state machine: now-playing, scrobble eligibility, durable retry queue, sweep of abandoned plays |
| `storage.py` | SQLite via `aiosqlite`; Last.fm session keys encrypted at rest with Fernet |
| `scrobblers/lastfm.py` | Last.fm API client (now-playing, scrobble, love, OAuth) |
| `linking.py` | `!fm link` / `!fm confirm` OAuth token dance |
| `commands.py` | Command routing |
| `matrix_bot_common.cross_signing` | Bootstrap and upload Matrix cross-signing keys (shared library) |
| `auto_accept.py` | Room invite auto-acceptance (trusted-sender allowlist) |

The bot listens for `dev.elementcall.musicbot.track_started` and `dev.elementcall.musicbot.track_finished` custom room events emitted by the companion music bot. These event types must match what the music bot is configured to emit.

## License

MIT — see [LICENSE](LICENSE).
