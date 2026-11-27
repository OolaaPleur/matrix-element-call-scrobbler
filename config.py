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
    """Settings from config/config.toml (optional) on top of data/credentials.json
    (scripts/setup.py): a value set in the config wins."""

    def __init__(self, path: str = "config/config.toml"):
        config_path = Path(path)
        data = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
        stored = load_credentials()

        m = data.get("matrix", {})
        self.homeserver   = m.get("homeserver") or stored.get("homeserver")
        self.user_id      = m.get("user_id") or stored.get("user_id")
        # Only needed when there is no saved token (data/access_token) yet.
        self.password     = m.get("password")
        self.device_name  = m.get("device_name", "scrobbler")
        owners = stored.get("owners", [])
        self.auto_accept_users = m.get("verification", {}).get("auto_accept_users", owners)

        lf = data.get("lastfm", {})
        self.lastfm_api_key       = lf.get("api_key") or stored.get("lastfm_api_key")
        self.lastfm_shared_secret = lf.get("shared_secret") or stored.get("lastfm_shared_secret")

        st = data.get("storage", {})
        self.encryption_key = st.get("encryption_key") or stored.get("encryption_key")
        self.db_path        = st.get("db_path", "data/scrobbler.db")

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
            ("matrix.homeserver", self.homeserver), ("matrix.user_id", self.user_id),
            ("lastfm.api_key", self.lastfm_api_key), ("lastfm.shared_secret", self.lastfm_shared_secret),
            ("storage.encryption_key", self.encryption_key),
        ] if not value]
        if missing:
            raise SystemExit("Missing settings: " + ", ".join(missing)
                             + ". Run scripts/setup.py first (see the README), or set them in the config.")
