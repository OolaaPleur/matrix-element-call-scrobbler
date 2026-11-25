import hashlib
import logging
from typing import Optional

import aiohttp

from .base import TrackInfo, UserCreds

logger = logging.getLogger(__name__)

LASTFM_API = "https://ws.audioscrobbler.com/2.0/"

# https://www.last.fm/api/errorcodes
ERR_INVALID_SESSION = 9
ERR_RATE_LIMIT = 29
# The request itself is wrong (invalid parameters / resource): a retry can't succeed.
# Everything else (outages, rate limit, a bad or suspended API key that may get fixed)
# goes through the normal retry backoff.
PERMANENT_ERRORS = {6, 7}


class LastFmError(Exception):
    """An error response from the Last.fm API (JSON body with "error" and "message")."""

    def __init__(self, code: int, message: str, http_status: int):
        super().__init__(f"Last.fm error {code} (HTTP {http_status}): {message}")
        self.code = code
        self.http_status = http_status

    @property
    def invalid_session(self) -> bool:
        return self.code == ERR_INVALID_SESSION

    @property
    def retryable(self) -> bool:
        return self.code not in PERMANENT_ERRORS


class ScrobbleIgnored(Exception):
    """Last.fm accepted the request but ignored the scrobble (ignoredMessage.code).

    1/2 artist/track ignored, 3/4 timestamp too old/new: final. 5 daily scrobble limit
    exceeded: try again later.
    """

    def __init__(self, code: str, message: str):
        super().__init__(f"Last.fm ignored scrobble (code {code}): {message}")
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.code == "5"


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

    @staticmethod
    async def _read(resp: aiohttp.ClientResponse) -> dict:
        # Last.fm reports errors as a JSON body {"error": <code>, "message": ...}, usually
        # with a 4xx/5xx status; the code says whether it's the session, the API key or
        # a temporary outage, which the HTTP status alone doesn't.
        try:
            data = await resp.json(content_type=None)
        except Exception:
            data = None
        if isinstance(data, dict) and "error" in data:
            raise LastFmError(int(data["error"]), str(data.get("message", "")), resp.status)
        resp.raise_for_status()
        if not isinstance(data, dict):
            raise RuntimeError(f"Last.fm returned a non-JSON response (HTTP {resp.status})")
        return data

    async def _post(self, params: dict) -> dict:
        await self._ensure_session()
        params["api_sig"] = _sign(params, self._secret)
        logger.debug("Last.fm POST method=%s", params.get("method"))
        async with self._session.post(LASTFM_API, data=params) as resp:
            return await self._read(resp)

    async def _get(self, params: dict) -> dict:
        await self._ensure_session()
        logger.debug("Last.fm GET method=%s", params.get("method"))
        async with self._session.get(LASTFM_API, params=params) as resp:
            return await self._read(resp)

    @staticmethod
    def _track_params(track: TrackInfo) -> dict:
        p = {"artist": track.artist, "track": track.track}
        if track.album:
            p["album"] = track.album
        if track.duration_s:
            p["duration"] = str(track.duration_s)
        return p

    # ── Scrobbler protocol ──────────────────────────────────────────────────

    async def now_playing(self, creds: UserCreds, track: TrackInfo) -> None:
        params = self._base_params("track.updateNowPlaying", sk=creds.secret)
        params.update(self._track_params(track))
        await self._post(params)

    async def scrobble(self, creds: UserCreds, track: TrackInfo) -> None:
        params = self._base_params("track.scrobble", sk=creds.secret)
        params.update(self._track_params(track))
        params["timestamp"] = str(track.timestamp)
        data = await self._post(params)
        scrobbles = data.get("scrobbles", {})
        if int(scrobbles.get("@attr", {}).get("accepted", 0)) == 0:
            scrobble = scrobbles.get("scrobble", {})
            if isinstance(scrobble, list):  # a batch; we always send one
                scrobble = scrobble[0] if scrobble else {}
            ignored = scrobble.get("ignoredMessage", {}) if isinstance(scrobble, dict) else {}
            raise ScrobbleIgnored(str(ignored.get("code", "?")), str(ignored.get("#text", "")))
        logger.info("Scrobbled '%s' - '%s' for %s", track.artist, track.track, creds.username)

    async def love(self, creds: UserCreds, track: TrackInfo) -> None:
        params = self._base_params("track.love", sk=creds.secret)
        params.update({"artist": track.artist, "track": track.track})
        await self._post(params)

    # ── Linking ─────────────────────────────────────────────────────────────

    async def start_linking(self, matrix_user_id: str) -> tuple[str, str]:
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
