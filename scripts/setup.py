#!/usr/bin/env python3
"""First-time setup of the scrobbler, in one run.

Asks for everything the bot needs and keeps it in data/credentials.json
(mode 600), so the config is optional and holds no secrets:

1. the bot's Matrix login: logs in once with the password (the password is
   not stored; the token goes to data/access_token);
2. your Matrix user ID (the bot joins rooms you invite it to; Enter to skip,
   more users in the config) and the music bot's user ID (whose track events are scrobbled);
3. the Last.fm API key and shared secret;
4. the key that encrypts the Last.fm sessions in the database (new, or the
   one of an earlier install whose scrobbler.db you keep);
5. cross-signing: if the account already has keys in secret storage (set up
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
TOKEN_FILE = DATA / "access_token"
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


def ask(prompt, secret=False, default=None):
    value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    if not value and default is not None:
        return default
    if not value:
        sys.exit("Cancelled")
    return value


def ask_user_id(prompt):
    value = ask(prompt)
    if not value.startswith("@") or ":" not in value:
        sys.exit(f"A full user ID is needed: @name:server (got {value!r})")
    return value


def step_login(stored):
    token = TOKEN_FILE.read_text().strip() if TOKEN_FILE.is_file() else ""
    if stored.get("homeserver") and stored.get("user_id") and token:
        try:
            request("GET", f"{stored['homeserver']}/_matrix/client/v3/account/whoami", token=token)
            print(f"1. Login: already done ({stored['user_id']}).")
            return token
        except MatrixError as exc:
            if exc.status != 401:
                sys.exit(f"Checking the saved login failed: {exc}")
            print("1. Login: the saved token no longer works (device logged out?), logging in again.")
    user_id = stored.get("user_id") or ask_user_id("Scrobbler bot's Matrix user ID (e.g. @scrobbler:matrix.org): ")
    homeserver = stored.get("homeserver") or discover_homeserver(user_id.split(":", 1)[1])
    print(f"Homeserver: {homeserver}")
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
    for path, value in ((TOKEN_FILE, resp["access_token"]), (DEVICE_ID_FILE, resp["device_id"])):
        path.write_text(value)
        path.chmod(0o600)
    stored.update({"homeserver": homeserver, "user_id": resp["user_id"]})
    bot_config.save_credentials(stored)
    print(f"1. Login: done, device {resp['device_id']}.")
    return resp["access_token"]


def step_settings(stored):
    changed = False
    if "owners" not in stored:
        owner = input("Your Matrix user ID; the bot joins rooms you invite it to "
                      "(Enter to skip; more users go into the config): ").strip()
        if owner and (not owner.startswith("@") or ":" not in owner):
            sys.exit(f"A full user ID is needed: @name:server (got {owner!r})")
        stored["owners"] = [owner] if owner else []
        changed = True
    if not stored.get("music_bots"):
        stored["music_bots"] = [ask_user_id("The music bot's Matrix user ID: ")]
        changed = True
    if not (stored.get("lastfm_api_key") and stored.get("lastfm_shared_secret")):
        print("Last.fm API account (https://www.last.fm/api/account/create):")
        stored["lastfm_api_key"] = ask("  API key: ")
        stored["lastfm_shared_secret"] = ask("  Shared secret (not shown): ", secret=True)
        changed = True
    if not stored.get("encryption_key"):
        from cryptography.fernet import Fernet
        key = ask("Storage encryption key (Enter for a new one; paste the old key to keep "
                  "an earlier scrobbler.db): ", secret=True, default="")
        if key:
            try:
                Fernet(key.encode())
            except ValueError:
                sys.exit("That isn't a valid key (Fernet, 44 characters).")
        stored["encryption_key"] = key or Fernet.generate_key().decode()
        changed = True
    if changed:
        bot_config.save_credentials(stored)
    print(f"2. Settings: owner {', '.join(stored['owners']) or 'none (the bot joins no room by itself)'}, music bot {', '.join(stored['music_bots'])}, "
          "Last.fm and storage keys saved.")


def step_cross_signing(stored, token):
    if (DATA / "cross_signing_keys.json").exists():
        print("3. Cross-signing keys: already there.")
        return True
    try:
        from matrix_bot_common import restore_keys
    except ImportError as exc:
        print(f"3. Cross-signing keys: matrix_bot_common is missing ({exc}); install requirements.txt.")
        return False
    print("3. Cross-signing keys: checking the account's secret storage...")
    ask_key = lambda: getpass.getpass("Recovery key from Element (not shown; Enter to skip): ")  # noqa: E731
    while True:
        try:
            result = restore_keys.restore(stored["homeserver"], token, DATA, ask_key)
            break
        except restore_keys.Abort as exc:
            print(f"   {exc}")
            if "recovery key" not in str(exc).lower():
                return False
    if result == "restored":
        print("   Restored: the bot signs its device on first start.")
    elif result == "fresh":
        print("   The account has no keys yet: the bot creates them on first start.")
    elif result == "no_secret_storage":
        print("   The account has keys but no recovery set up, so they can't be restored. The bot will run "
              "with an unsigned device; to fix it, replace the keys (README, Device Verification).")
    else:
        print("   Skipped. Without the keys the bot can't sign its device; see the README "
              "(Device Verification) to restore them later or to replace them.")
    return True


def main():
    stored = bot_config.load_credentials()
    token = step_login(stored)
    step_settings(stored)
    keys_ok = step_cross_signing(stored, token)
    if not keys_ok:
        sys.exit("\nSetup isn't complete: fix the problem above and run scripts/setup.py again.")
    print("\nSetup done. Start the bot: `docker compose up -d` (or `python3 main.py`).")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nCancelled")
