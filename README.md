# Matrix Element Call Scrobbler

A Matrix bot that listens for playback events from [matrix-element-call-musicbot](https://github.com/OolaaPleur/matrix-element-call-musicbot) and scrobbles tracks to Last.fm on behalf of linked users.

Multiple users in the same Element Call session each get their own scrobbles to their own Last.fm account — the bot handles all of them in parallel.

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- Setup: [with Docker](#run-with-docker) · [without Docker](#run-without-docker) · [optional settings](#optional-settings)
- [Who can use the bot?](#who-can-use-the-bot)
- [User commands](#user-commands)
- [Device verification](#device-verification)
- [Filtering playback events](#filtering-playback-events)
- [Architecture](#architecture)

## How It Works

The scrobbler pairs with [matrix-element-call-musicbot](https://github.com/OolaaPleur/matrix-element-call-musicbot). When the music bot starts playing a track in an Element Call session, it emits custom Matrix room events carrying the track metadata and the list of active call participants. The scrobbler:

1. Immediately updates "now playing" on Last.fm for every linked participant
2. Scrobbles the track when it finishes, subject to Last.fm's eligibility rules (≥30 s played and ≥50% of duration, or ≥240 s)
3. Retries failed scrobbles with exponential backoff (up to 5 attempts over several hours); scrobbles Last.fm ignores or rejects as invalid are dropped, an expired session disables the user and tells them in the room
4. Persists the retry queue in SQLite so scrobbles survive bot restarts

## Requirements

- Docker, or Python 3.11+ to run it directly
- A Matrix account for the bot (any homeserver)
- A [Last.fm API account](https://www.last.fm/api/account/create) (free) — you need an API key and shared secret
- [matrix-element-call-musicbot](https://github.com/OolaaPleur/matrix-element-call-musicbot) running in the same room(s), with `scrobbler_events = true` in the `[bot]` section of its `config.toml` (the music bot doesn't send track events by default)

## Setup

There are two ways to run the bot: the prebuilt Docker image (recommended) or directly on the host. Either way, `scripts/setup.py` asks once for everything the bot needs:

- the bot's Matrix user ID and password (it logs in once; the password is not stored),
- your Matrix user ID (the bot joins rooms you invite it to; Enter to skip, more users go into `auto_accept_room_invites_from` in the optional config) and the music bot's user ID (whose track events are scrobbled),
- the API key and shared secret of your [Last.fm API account](https://www.last.fm/api/account/create),
- the key that encrypts the linked Last.fm accounts in the database (Enter for a new one),
- the bot account's recovery key, if the account was set up in Element (see [Device Verification](#device-verification)).

It keeps them in `data/credentials.json` (mode 600), so there is no config to fill in. Run it again if a step failed: done steps are skipped.

### Run with Docker

The bot runs from the prebuilt image `ghcr.io/oolaapleur/matrix-element-call-scrobbler` (amd64 and arm64), published by CI for every change; the clone is only for `docker-compose.yml` and the optional config.

```bash
git clone https://github.com/OolaaPleur/matrix-element-call-scrobbler
cd matrix-element-call-scrobbler

docker compose run --rm scrobbler python3 scripts/setup.py   # asks for the values above
docker compose up -d             # start the bot
docker compose logs -f           # watch it start (Ctrl+C to stop watching)
```

Update to the latest version:

```bash
git pull
docker compose pull
docker compose up -d
```

- Bot data (setup values, SQLite DB with the linked accounts and the scrobble queue, device ID, crypto store, access token, cross-signing keys) lives in the `scrobbler_data` volume, logs in `scrobbler_logs`. **Back up `scrobbler_data`**: without it the bot logs in as a new device that has to be verified again, and every user has to link Last.fm again.
- Build it yourself (e.g. with your own changes): uncomment `build: .` in `docker-compose.yml` and run `docker compose up -d --build`; the build fetches the shared library from GitHub, so no second clone is needed.
- Windows: not tested. Docker Desktop runs the same Linux image, so the same commands should work in PowerShell.

### Run without Docker

Requires Python 3.11+.

```bash
# The shared library must be cloned next to this repo
git clone https://github.com/OolaaPleur/matrix-element-call-common
git clone https://github.com/OolaaPleur/matrix-element-call-scrobbler
cd matrix-element-call-scrobbler

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # resolves ../matrix-element-call-common automatically

python scripts/setup.py
python main.py
```

> [!NOTE]
> `requirements.txt` references `../matrix-element-call-common` (the [shared library](https://github.com/OolaaPleur/matrix-element-call-common)). Both repos must be cloned into the same parent directory.

Logs go to `logs/scrobbler.log` (INFO and up, rotating, 10 MB × 5 files). SIGTERM/Ctrl+C stop the bot cleanly.

To keep it running, a systemd user service (`~/.config/systemd/user/matrix-scrobbler.service`; adjust the paths to where you cloned it):

```ini
[Unit]
Description=Matrix Element Call scrobbler
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=%h/matrix-element-call-scrobbler
ExecStart=%h/matrix-element-call-scrobbler/.venv/bin/python main.py
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now matrix-scrobbler
loginctl enable-linger "$USER"   # keep it running while you're logged out
```

Windows: not tested. The bot is plain Python, so the same steps should work in PowerShell (`.venv\Scripts\Activate.ps1` instead of `source .venv/bin/activate`); reports welcome.

### Optional settings

The [filters](#filtering-playback-events) and timings live in an optional config: `cp config/config.toml.example config/config.toml` and edit it (mounted read-only into the container; restart the bot after a change). A value set there overrides the one from `setup.py`.

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

The bot signs its own device with the account's cross-signing keys. If the account was set up in Element (the usual case: you created it there and set up recovery), it already has keys, and `setup.py` restores them with the account's **recovery key**. A fresh account gets its keys from the bot on first start.

To restore them later (skipped in setup, lost `data/`), run this and restart the bot:

```bash
docker compose exec scrobbler python3 -m matrix_bot_common.restore_keys
python -m matrix_bot_common.restore_keys        # without Docker, in the bot's directory
```

**No recovery key?** The bot can replace the account's keys, but only when asked to: start it once with `CROSS_SIGNING_ALLOW_RESET=1` (Docker: add `environment: [CROSS_SIGNING_ALLOW_RESET=1]` to the service, remove it afterwards). On homeservers with OAuth login (matrix.org) the log then shows a link: open it, log in as the bot, approve the reset, and restart the bot. Everyone who verified the bot has to verify it again.

**After a reinstall** (or a lost `data/`), log the bot's old sessions out: in Element logged in as the bot, Settings → Sessions, sign out every session but the current ones. They no longer work, but everyone's clients keep sending them room keys, and unsigned ones can show as unverified sessions of the bot.

**Verifying the bot (recommended)** adds the green shield in Element. Log in to Element as the bot in a separate browser or private window (skip this if it is still logged in from when you created the account; if Element asks to verify that session, use the recovery key, **never "Reset"**). Then in your own Element open the bot's profile → **Verify**, accept in the bot's session and compare the emoji. You can log that session out afterwards.

## Filtering Playback Events

The bot can be configured to only scrobble certain kinds of tracks. All filters are in the optional `config/config.toml` (see [Optional settings](#optional-settings)):

| Section | Key | Effect |
|---------|-----|--------|
| `[emitters]` | `allowed_user_ids` | Allowlist of music bot Matrix IDs, checked against the event's sender (`["*"]` = anyone in the room, who could then scrobble anything to linked accounts) |
| `[sources]` | `allowed` / `denied` | Allowlist/denylist of source strings (`"youtube_music"`, `"youtube"`, `"other"`) |
| `[quality]` | `require_high` | If `true`, only scrobble when the music bot provided artist/track from file tags (not title-parsed heuristics) |

Events with an empty artist or track name are always dropped.

## Architecture

| File | Purpose |
|------|---------|
| `bot.py` | Startup, E2EE setup, sync loop with exponential backoff |
| `scripts/setup.py` | One-time setup: login, IDs, Last.fm and storage keys into `data/credentials.json`, cross-signing key restore |
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
