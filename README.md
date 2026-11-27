# Matrix Element Call Scrobbler

A Matrix bot that scrobbles what [matrix-element-call-musicbot](https://github.com/OolaaPleur/matrix-element-call-musicbot) plays to Last.fm, for everyone in the Element Call: each linked user gets the scrobbles on their own Last.fm account.

## Contents

- [How It Works](#how-it-works)
- [Setup](#setup) · [optional settings](#optional-settings) · [without Docker](docs/install-raw.md)
- [User Commands](#user-commands)
- [Access and Filters](#access-and-filters)
- [Device Verification](#device-verification)
- Developer notes: [CLAUDE.md](CLAUDE.md)

## How It Works

The music bot sends a room event when a track starts and ends, with the track and the call's participants. The scrobbler sets "now playing" on Last.fm for every linked participant and scrobbles the track when it ends (≥30 s played and ≥50% of it, or ≥240 s). Failed scrobbles are retried for several hours and survive restarts.

## Setup

You need:

- a Matrix account for the bot and Docker;
- a [Last.fm API account](https://www.last.fm/api/account/create) (free): its API key and shared secret;
- the music bot in the same room(s), with `scrobbler_events = true` in the `[bot]` section of its `config.toml` (off by default).

`scripts/setup.py` asks once for everything: the bot's user ID and password (the password is not stored), your user ID (the bot joins rooms you invite it to; Enter to skip), the music bot's user ID, the Last.fm key and secret, the key that encrypts the linked Last.fm accounts (Enter for a new one) and the account's recovery key ([Device Verification](#device-verification)). It keeps them in `data/credentials.json` (mode 600), so there is no config to fill in. Run it again if a step failed: done steps are skipped.

The bot runs from the prebuilt image `ghcr.io/oolaapleur/matrix-element-call-scrobbler` (amd64 and arm64); the clone only provides `docker-compose.yml` and the example config.

```bash
git clone https://github.com/OolaaPleur/matrix-element-call-scrobbler
cd matrix-element-call-scrobbler

cp config/config.example.toml config/config.toml
nano config/config.toml          # settings (optional, the defaults work)

docker compose run --rm scrobbler python3 scripts/setup.py   # asks for the values above
docker compose up -d             # start the bot
docker compose logs -f           # watch it start (Ctrl+C to stop watching)
```

Then invite the bot to the music bot's room and type `!fm link`.

Update to the latest version:

```bash
git pull
docker compose pull
docker compose up -d
```

- **Back up the `scrobbler_data` volume** (setup values, linked accounts, scrobble queue, E2EE and cross-signing keys): without it the bot logs in as a new device and every user has to link Last.fm again.
- Windows: not tested. Docker Desktop runs the same Linux image, so the same commands should work in PowerShell.
- Build it yourself: uncomment `build: .` in `docker-compose.yml` and run `docker compose up -d --build`.
- Without Docker: [docs/install-raw.md](docs/install-raw.md), including a systemd unit.

### Optional settings

Filters and timings live in `config/config.toml` (copied from `config/config.example.toml`); restart the bot after a change. A value set there overrides the one from `setup.py`.

## User Commands

Any user in a room with the bot can link their own Last.fm account; they never share credentials with the bot owner.

| Command | Description |
|---------|-------------|
| `!fm link` | Start linking: returns a Last.fm URL to open in your browser |
| `!fm confirm` | Finish linking after authorizing at that URL |
| `!fm unlink` | Unlink your Last.fm account |
| `!fm status` | Show whether your Last.fm account is linked |
| `!fm love` | Love the most recently scrobbled track |
| `!fm blacklist` | Stop scrobbling for you in this room |
| `!fm whitelist` | Scrobble for you in this room again |

## Access and Filters

The bot joins rooms only on invites from `auto_accept_room_invites_from` (setup puts your user ID there; list more users in the optional config). In the optional config you can also limit what gets scrobbled:

| Section | Key | Effect |
|---------|-----|--------|
| `[emitters]` | `allowed_user_ids` | Music bots whose events count (setup puts the music bot there; `["*"]` = anyone in the room, who could then scrobble anything to linked accounts) |
| `[sources]` | `allowed` / `denied` | Sources to allow or deny: `"youtube_music"`, `"youtube"`, `"other"` |
| `[quality]` | `require_high` | `true` = only tracks whose artist/title came from file tags, not guessed from the video title |

Events with an empty artist or track name are always dropped.

## Device Verification

The bot signs its own device with the account's cross-signing keys. `setup.py` takes care of them:

- **The account was set up in Element** (the usual case: Element asked you to set up recovery): setup restores the keys with that **recovery key**.
- **A fresh account:** the bot creates the keys on first start.
- **To restore them later** (skipped in setup, lost data): `docker compose exec scrobbler python3 -m matrix_bot_common.restore_keys`, then restart the bot.
- **Keys but no recovery key:** start the bot once with `CROSS_SIGNING_ALLOW_RESET=1` (add `environment: [CROSS_SIGNING_ALLOW_RESET=1]` to the service, remove it afterwards). On matrix.org the log then shows a link: open it, log in as the bot, approve the reset, restart the bot. Everyone who verified the bot has to verify it again.

**After a reinstall** (or lost data), log the bot's old sessions out: in Element logged in as the bot, Settings → Sessions, sign out every session but the current one. Otherwise they can show as unverified sessions of the bot.

**Verifying the bot (recommended)** adds the green shield. Log in to Element as the bot in a separate browser or private window (skip this if it is still logged in; if Element asks to verify that session, use the recovery key, **never "Reset"**). Then in your own Element open the bot's profile → **Verify**, accept in the bot's session and compare the emoji. You can log that session out afterwards.

## License

MIT — see [LICENSE](LICENSE).
