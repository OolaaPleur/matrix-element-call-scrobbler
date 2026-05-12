import hashlib
import logging
import time
from typing import Optional

import aiohttp

from .base import TrackInfo, UserCreds

logger = logging.getLogger(__name__)

LASTFM_API = "https://ws.audioscrobbler.com/2.0/"


def _sign(params: dict, shared_secret: str) -> str:
    filtered = {k: v for k, v in params.items() if k != "format"}
    sig_str = "".join(f"{k}{v}" for k, v in sorted(filtered.items()))
    sig_str += shared_secret
    return hashlib.md5(sig_str.encode("utf-8")).hexdigest()


class LastFmScrobbler:
    service_name = "lastfm"

    def __init__(self, api_key: str, shared_secret: str, session: Optional[aiohttp.ClientSession] = None):
        self._api_key = api_key
        self._secret = shared_secret
        self._session = session
        self._own_session = session is None

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True

    async def close(self):
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()

    def _base_params(self, method: str, sk: Optional[str] = None) -> dict:
        p = {"method": method, "api_key": self._api_key, "format": "json"}
        if sk:
            p["sk"] = sk
        return p

    async def _post(self, params: dict) -> dict:
        await self._ensure_session()
        params["api_sig"] = _sign(params, self._secret)
        logger.debug("Last.fm POST method=%s", params.get("method"))
        async with self._session.post(LASTFM_API, data=params) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _get(self, params: dict) -> dict:
        await self._ensure_session()
        logger.debug("Last.fm GET method=%s", params.get("method"))
        async with self._session.get(LASTFM_API, params=params) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    # ── Scrobbler protocol ──────────────────────────────────────────────────

    async def now_playing(self, creds: UserCreds, track: TrackInfo) -> None:
        try:
            params = self._base_params("track.updateNowPlaying", sk=creds.secret)
            params.update({"artist": track.artist, "track": track.track})
            if track.album:
                params["album"] = track.album
            if track.duration_s:
                params["duration"] = str(track.duration_s)
            await self._post(params)
        except Exception:
            logger.debug("now_playing failed (ignored)", exc_info=True)

    async def scrobble(self, creds: UserCreds, track: TrackInfo) -> None:
        params = self._base_params("track.scrobble", sk=creds.secret)
        params.update({
            "artist": track.artist,
            "track": track.track,
            "timestamp": str(track.timestamp),
        })
        if track.album:
            params["album"] = track.album
        if track.duration_s:
            params["duration"] = str(track.duration_s)
        data = await self._post(params)
        accepted = data.get("scrobbles", {}).get("@attr", {}).get("accepted", 0)
        if int(accepted) == 0:
            ignored_msg = data.get("scrobbles", {}).get("scrobble", {}).get("ignoredMessage", {})
            raise RuntimeError(f"Last.fm ignored scrobble: {ignored_msg}")
        logger.info("Scrobbled '%s' - '%s' for %s", track.artist, track.track, creds.username)

    async def love(self, creds: UserCreds, track: TrackInfo) -> None:
        params = self._base_params("track.love", sk=creds.secret)
        params.update({"artist": track.artist, "track": track.track})
        await self._post(params)

    # ── Linking ─────────────────────────────────────────────────────────────

    async def start_linking(self, matrix_user_id: str) -> str:
        params = {"method": "auth.getToken", "api_key": self._api_key, "format": "json"}
        params["api_sig"] = _sign(params, self._secret)
        data = await self._get(params)
        token = data["token"]
        return token, f"https://www.last.fm/api/auth/?api_key={self._api_key}&token={token}"

    async def finalize_linking(self, request_token: str) -> UserCreds:
        params = self._base_params("auth.getSession")
        params["token"] = request_token
        data = await self._post(params)
        session = data["session"]
        return UserCreds(
            service="lastfm",
            username=session["name"],
            secret=session["key"],
        )
