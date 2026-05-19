import logging
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

logger = logging.getLogger(__name__)


class Config:
    def __init__(self, path: str = "config/config.toml"):
        raw = Path(path).read_bytes()
        data = tomllib.loads(raw.decode())

        m = data["matrix"]
        self.homeserver   = m["homeserver"]
        self.user_id      = m["user_id"]
        self.password     = m["password"]
        self.device_name  = m.get("device_name", "scrobbler")
        self.auto_accept_users = m.get("verification", {}).get("auto_accept_users", [])

        lf = data["lastfm"]
        self.lastfm_api_key       = lf["api_key"]
        self.lastfm_shared_secret = lf["shared_secret"]

        st = data["storage"]
        self.encryption_key = st["encryption_key"]
        self.db_path        = st.get("db_path", "data/scrobbler.db")

        bh = data.get("behavior", {})
        self.auto_accept_room_invites_from = bh.get("auto_accept_room_invites_from", [])
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
        self.emitters_allowed_user_ids = em.get("allowed_user_ids", ["*"])

        ql = data.get("quality", {})
        self.quality_require_high = ql.get("require_high", False)
