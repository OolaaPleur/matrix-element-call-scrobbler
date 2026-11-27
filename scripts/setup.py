#!/usr/bin/env python3
"""First-time setup of the scrobbler, in one run.

Asks for everything the bot needs and keeps it in data/credentials.json
(mode 600), so the config is optional and holds no secrets:

1. the bot's Matrix login: logs in once with the password (the password is
   not stored);
2. your Matrix user ID: the bot joins rooms you invite it to (Enter to skip,
   more users in the config);
3. the music bot's user ID (whose track events are scrobbled);
4. the Last.fm API key and shared secret;
5. the key that encrypts the Last.fm sessions in the database (new, or the
   one of an earlier install whose scrobbler.db you keep);
6. cross-signing: if the account already has keys in secret storage (set up
   in Element), asks for the recovery key and restores them, so the bot signs
   its device on first start.

Safe to run again: done steps are skipped. When everything is done, it
offers to change the answers (another bot account, other users, another
music bot, other Last.fm API keys, the cross-signing keys restored again).
Stop the bot first.

    docker compose run --rm scrobbler python3 scripts/setup.py
    python3 scripts/setup.py            # without Docker, in the bot's venv
"""
import datetime
import getpass
import json
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEVICE_ID_FILE = DATA / "device_id"
sys.path.insert(0, str(ROOT))
import config as bot_config  # noqa: E402

DEVICE_NAME = "scrobbler"
# Cloudflare in front of some homeservers (matrix.org) rejects urllib's default User-Agent.
USER_AGENT = "matrix-element-call-scrobbler"


class MatrixError(Exception):
    def __init__(self, status, errcode, message):
        super().__init__(f"HTTP {status} {errcode or ''} {message or ''}".strip())
        self.status = status


def request(method, url, body=None, token=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            err = json.load(exc)
        except ValueError:
            err = {}
        raise MatrixError(exc.code, err.get("errcode"), err.get("error")) from None


def discover_homeserver(server_name):
    try:
        base = request("GET", f"https://{server_name}/.well-known/matrix/client")["m.homeserver"]["base_url"]
        if base:
            return base.rstrip("/")
    except (MatrixError, urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        pass
    return f"https://{server_name}"


INDENT = "   "


def say(text):
    print(INDENT + text)


WORKED = False  # set when a step did something on this run


def done(text):
    global WORKED
    WORKED = True
    print(INDENT + "✓ " + text)


def already(text):
    print(INDENT + "✓ " + text)


def yes(prompt):
    return input(INDENT + prompt + " [y/N]: ").strip().lower() in ("y", "yes")


def user_ids(text):
    return [check_user_id(part) for part in text.replace(",", " ").split() if part]


def config_overrides(section, keys):
    """Keys of config/config.toml's [section] that override what setup stores."""
    path = ROOT / "config" / "config.toml"
    if not path.is_file():
        return []
    table = tomllib.loads(path.read_text(encoding="utf-8")).get(section, {})
    return [key for key in keys if key in table]


def ask_ids(stored, key, section, config_key, empty):
    """Change mode: new user IDs for stored[key], unless the config overrides them."""
    if config_overrides(section, [config_key]):
        say(f"{config_key} in [{section}] of the config overrides this; change it there.")
        return
    answer = input(INDENT + f"Enter to keep, {'- for nobody, ' if empty else ''}"
                            "or user IDs (space-separated): ").strip()
    if not answer:
        return
    stored[key] = [] if answer == "-" and empty else user_ids(answer)
    if not stored[key] and not empty:
        sys.exit("At least one user ID is needed")
    bot_config.save_credentials(stored)
    done(", ".join(stored[key]) or "nobody")


# What belongs to the bot's account, moved aside when it changes. The database
# (linked Last.fm accounts, scrobble queue) stays.
ACCOUNT_FILES = ["crypto_store", "device_id", "cross_signing_keys.json",
                 "cross_signing_keys.json.stale-*"]


def move_account_files():
    target = DATA / f"previous-account-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    moved = [path for pattern in ACCOUNT_FILES for path in DATA.glob(pattern)]
    if moved:
        target.mkdir()
        for path in moved:
            path.rename(target / path.name)
        say(f"The old account's keys and device are in data/{target.name}/")


def ask(prompt, secret=False, default=None):
    prompt = INDENT + prompt
    value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    if not value and default is not None:
        return default
    if not value:
        sys.exit("Cancelled")
    return value


def check_user_id(value):
    if not value.startswith("@") or ":" not in value or any(c in value for c in " ,"):
        sys.exit(f"A full user ID is needed: @name:server (got {value!r})")
    return value


def step_login(stored, change=False):
    print("1. Login")
    token = stored.get("access_token")
    if stored.get("homeserver") and stored.get("user_id") and token:
        try:
            request("GET", f"{stored['homeserver']}/_matrix/client/v3/account/whoami", token=token)
            already(f"{'Now' if change else 'Already done'}: {stored['user_id']}")
            if not change:
                return token
            new_id = input(INDENT + "Enter to keep, or another account's user ID: ").strip()
            if not new_id or new_id == stored["user_id"]:
                return token
            check_user_id(new_id)
            say("The bot's keys and device belong to its account: they are moved aside, and the next")
            say("steps set up the new account. Linked Last.fm accounts stay.")
            if not yes(f"Switch the bot to {new_id}?"):
                return token
            move_account_files()
            for key in ("homeserver", "user_id", "access_token"):
                stored.pop(key, None)
            stored["user_id"] = new_id
            bot_config.save_credentials(stored)
        except MatrixError as exc:
            if exc.status != 401:
                sys.exit(f"Checking the saved login failed: {exc}")
            say("The saved token no longer works (device logged out?), logging in again.")
    user_id = stored.get("user_id") or check_user_id(ask("Scrobbler bot's Matrix user ID (e.g. @scrobbler:matrix.org): "))
    homeserver = stored.get("homeserver") or discover_homeserver(user_id.split(":", 1)[1])
    say(f"Homeserver: {homeserver}")
    try:
        flows = request("GET", f"{homeserver}/_matrix/client/v3/login").get("flows", [])
        if not any(flow.get("type") == "m.login.password" for flow in flows):
            sys.exit("This homeserver doesn't allow password login for bots (no m.login.password).")
        resp = request("POST", f"{homeserver}/_matrix/client/v3/login", {
            "type": "m.login.password", "identifier": {"type": "m.id.user", "user": user_id},
            "password": ask("Bot's password (not shown, not stored): ", secret=True),
            "initial_device_display_name": DEVICE_NAME,
        })
    except MatrixError as exc:
        sys.exit(f"Login failed: {exc}")
    except OSError as exc:
        sys.exit(f"Can't reach {homeserver}: {exc}")
    DATA.mkdir(parents=True, exist_ok=True)
    DEVICE_ID_FILE.write_text(resp["device_id"])
    stored.update({"homeserver": homeserver, "user_id": resp["user_id"], "access_token": resp["access_token"]})
    bot_config.save_credentials(stored)
    done(f"Device {resp['device_id']}, saved in data/credentials.json")
    return resp["access_token"]


def step_owner(stored, change=False):
    print("2. Room invites")
    if "owners" in stored:
        already(f"{'Now' if change else 'Already done'}: accepted from "
                f"{', '.join(stored['owners']) or 'nobody (skipped)'}")
        if change:
            ask_ids(stored, "owners", "behavior", "auto_accept_room_invites_from", empty=True)
        return
    owner = input(INDENT + "Your Matrix user ID; the bot joins rooms you invite it to "
                  "(Enter to skip; more users: auto_accept_room_invites_from in the config): ").strip()
    if owner:
        check_user_id(owner)
    stored["owners"] = [owner] if owner else []
    bot_config.save_credentials(stored)
    if owner:
        done(f"Accepted from {owner}")
    else:
        done("Skipped: the bot joins no room by itself; set auto_accept_room_invites_from in the config")


def step_music_bot(stored, change=False):
    print("3. Music bot")
    if stored.get("music_bots"):
        already(f"{'Now' if change else 'Already done'}: scrobbling track events from "
                f"{', '.join(stored['music_bots'])}")
        if change:
            ask_ids(stored, "music_bots", "emitters", "allowed_user_ids", empty=False)
        return
    stored["music_bots"] = [check_user_id(ask("The music bot's Matrix user ID: "))]
    bot_config.save_credentials(stored)
    done(f"Scrobbling track events from {', '.join(stored['music_bots'])}")


def step_lastfm(stored, change=False):
    print("4. Last.fm")
    if stored.get("lastfm_api_key") and stored.get("lastfm_shared_secret"):
        already(f"{'Now' if change else 'Already done'}: API key {stored['lastfm_api_key'][:4]}…")
        if not change:
            return
        say("Linked accounts belong to the API key: with another one, everyone links Last.fm again.")
        if not yes("Use another API key?"):
            return
    say("From your API account: https://www.last.fm/api/account/create")
    stored["lastfm_api_key"] = ask("API key: ")
    stored["lastfm_shared_secret"] = ask("Shared secret (not shown): ", secret=True)
    bot_config.save_credentials(stored)
    done("Saved")


def step_storage_key(stored, change=False):
    print("5. Storage encryption key")
    if stored.get("encryption_key"):
        already("Already done" + (" (can't be changed: the linked accounts in scrobbler.db are "
                                  "encrypted with it)" if change else ""))
        return
    from cryptography.fernet import Fernet
    key = ask("Enter for a new key, or paste the old one to keep an earlier scrobbler.db (not shown): ",
              secret=True, default="")
    if key:
        try:
            Fernet(key.encode())
        except ValueError:
            sys.exit("That isn't a valid key (Fernet, 44 characters).")
    stored["encryption_key"] = key or Fernet.generate_key().decode()
    bot_config.save_credentials(stored)
    done("Old key kept" if key else "New key generated")


def step_cross_signing(stored, token, change=False):
    print("6. Cross-signing keys")
    if (DATA / "cross_signing_keys.json").exists():
        already("Already there")
        if not change or not yes("Restore them again with the recovery key (e.g. after a reset in Element)?"):
            return True
    try:
        from matrix_bot_common import restore_keys
    except ImportError as exc:
        say(f"matrix_bot_common is missing ({exc}); install requirements.txt.")
        return False
    ask_key = lambda: getpass.getpass(INDENT + "Recovery key from Element (not shown; Enter to skip): ")  # noqa: E731
    while True:
        try:
            result = restore_keys.restore(stored["homeserver"], token, DATA, ask_key, out=lambda *_: None)
            break
        except restore_keys.Abort as exc:
            say(str(exc))
            if "recovery key" not in str(exc).lower():
                return False
    if result == "restored":
        say("master, self-signing, user-signing: match the server")
        done("Restored: the bot signs its device on first start")
    elif result == "fresh":
        done("The account has no keys yet: the bot creates them on first start")
    elif result == "no_secret_storage":
        already("The account has keys but no recovery set up, so they can't be restored. The bot runs "
             "with an unsigned device until you replace them (README, Device Verification)")
    else:
        already("Skipped: the bot can't sign its device without the keys; restore or replace them later "
             "(README, Device Verification)")
    return True


def main():
    stored = bot_config.load_credentials()
    change = False
    while True:
        token = step_login(stored, change)
        step_owner(stored, change)
        step_music_bot(stored, change)
        step_lastfm(stored, change)
        step_storage_key(stored, change)
        keys_ok = step_cross_signing(stored, token, change)
        if not keys_ok:
            sys.exit("\nSetup isn't complete: fix the problem above and run scripts/setup.py again.")
        if WORKED or change:
            break
        print("\nEverything is set up.")
        if not yes("Change something?"):
            return
        print()
        change = True
    print("\nSetup done. Start the bot: docker compose up -d (or python3 main.py)"
          if not change else "\nDone. Start the bot again: docker compose up -d (or python3 main.py)")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nCancelled")
