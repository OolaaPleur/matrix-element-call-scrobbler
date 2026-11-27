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

Safe to run again: done steps are skipped.

    docker compose run --rm scrobbler python3 scripts/setup.py
    python3 scripts/setup.py            # without Docker, in the bot's venv
"""
import getpass
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LEGACY_TOKEN_FILE = DATA / "access_token"  # where older installs kept the token
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


def done(text):
    print(INDENT + "✓ " + text)


def ask(prompt, secret=False, default=None):
    prompt = INDENT + prompt
    value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    if not value and default is not None:
        return default
    if not value:
        sys.exit("Cancelled")
    return value


def check_user_id(value):
    if not value.startswith("@") or ":" not in value:
        sys.exit(f"A full user ID is needed: @name:server (got {value!r})")
    return value


def step_login(stored):
    print("1. Login")
    token = stored.get("access_token") or (
        LEGACY_TOKEN_FILE.read_text().strip() if LEGACY_TOKEN_FILE.is_file() else "")
    if stored.get("homeserver") and stored.get("user_id") and token:
        try:
            request("GET", f"{stored['homeserver']}/_matrix/client/v3/account/whoami", token=token)
            done(f"Already done: {stored['user_id']}")
            return token
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
    LEGACY_TOKEN_FILE.unlink(missing_ok=True)
    stored.update({"homeserver": homeserver, "user_id": resp["user_id"], "access_token": resp["access_token"]})
    bot_config.save_credentials(stored)
    done(f"Device {resp['device_id']}, saved in data/credentials.json")
    return resp["access_token"]


def step_owner(stored):
    print("2. Room invites")
    if "owners" in stored:
        done(f"Already done: accepted from {', '.join(stored['owners']) or 'nobody (skipped)'}")
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


def step_music_bot(stored):
    print("3. Music bot")
    if not stored.get("music_bots"):
        stored["music_bots"] = [check_user_id(ask("The music bot's Matrix user ID: "))]
        bot_config.save_credentials(stored)
    done(f"Scrobbling track events from {', '.join(stored['music_bots'])}")


def step_lastfm(stored):
    print("4. Last.fm")
    if stored.get("lastfm_api_key") and stored.get("lastfm_shared_secret"):
        done("Already done")
        return
    say("From your API account: https://www.last.fm/api/account/create")
    stored["lastfm_api_key"] = ask("API key: ")
    stored["lastfm_shared_secret"] = ask("Shared secret (not shown): ", secret=True)
    bot_config.save_credentials(stored)
    done("Saved")


def step_storage_key(stored):
    print("5. Storage encryption key")
    if stored.get("encryption_key"):
        done("Already done")
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


def step_cross_signing(stored, token):
    print("6. Cross-signing keys")
    if (DATA / "cross_signing_keys.json").exists():
        done("Already there")
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
        done("The account has keys but no recovery set up, so they can't be restored. The bot runs "
             "with an unsigned device until you replace them (README, Device Verification)")
    else:
        done("Skipped: the bot can't sign its device without the keys; restore or replace them later "
             "(README, Device Verification)")
    return True


def main():
    stored = bot_config.load_credentials()
    token = step_login(stored)
    step_owner(stored)
    step_music_bot(stored)
    step_lastfm(stored)
    step_storage_key(stored)
    keys_ok = step_cross_signing(stored, token)
    if not keys_ok:
        sys.exit("\nSetup isn't complete: fix the problem above and run scripts/setup.py again.")
    print("\nSetup done. Start the bot: docker compose up -d (or python3 main.py)")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nCancelled")
