import json
import logging
import os
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
# Written by scripts/setup.py: the bot's login (homeserver, user_id), the Last.fm
# keys, the storage key and the user IDs asked during setup. Kept in data/ rather
# than the config: it holds secrets, and data/ is the bot's own volume.
CREDENTIALS_FILE = ROOT / "data" / "credentials.json"


def load_credentials() -> dict:
    try:
        data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_credentials(data: dict) -> None:
    """Write readable by the owner only; the file never exists with wider permissions."""
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CREDENTIALS_FILE.with_name(CREDENTIALS_FILE.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, CREDENTIALS_FILE)


class Config:
    """data/credentials.json (scripts/setup.py) plus the optional config/config.toml;
    the room-invite and music-bot lists from setup can be replaced there."""

    def __init__(self, path: str = "config/config.toml"):
        config_path = Path(path)
        data = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
        stored = load_credentials()

        # The login and the keys come from setup only.
        self.homeserver           = stored.get("homeserver")
        self.user_id              = stored.get("user_id")
        self.access_token         = stored.get("access_token")
        self.lastfm_api_key       = stored.get("lastfm_api_key")
        self.lastfm_shared_secret = stored.get("lastfm_shared_secret")
        self.encryption_key       = stored.get("encryption_key")
        owners = stored.get("owners", [])

        self.db_path = data.get("storage", {}).get("db_path", "data/scrobbler.db")

        bh = data.get("behavior", {})
        self.auto_accept_room_invites_from = bh.get("auto_accept_room_invites_from", owners)
        self.recovery_lookback_hours       = bh.get("recovery_lookback_hours", 6)
        self.abandoned_play_grace_seconds  = bh.get("abandoned_play_grace_seconds", 60)
        self.queue_drain_interval_seconds  = bh.get("queue_drain_interval_seconds", 30)
        self.abandoned_sweep_interval_secs = bh.get("abandoned_sweep_interval_secs", 300)

        src = data.get("sources", {})
        self.sources_allowed = src.get("allowed", ["*"])
        self.sources_denied  = src.get("denied", [])
        if self.sources_allowed == []:
            logger.warning("config [sources] allowed is empty — scrobbler will never scrobble anything")

        em = data.get("emitters", {})
        self.emitters_allowed_user_ids = em.get("allowed_user_ids", stored.get("music_bots", ["*"]))

        ql = data.get("quality", {})
        self.quality_require_high = ql.get("require_high", False)

        missing = [name for name, value in [
            ("homeserver", self.homeserver), ("user_id", self.user_id), ("access_token", self.access_token),
            ("lastfm_api_key", self.lastfm_api_key), ("lastfm_shared_secret", self.lastfm_shared_secret),
            ("encryption_key", self.encryption_key),
        ] if not value]
        if missing:
            raise SystemExit("Missing in data/credentials.json: " + ", ".join(missing)
                             + ". Run scripts/setup.py first (see the README).")
