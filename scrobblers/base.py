from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass
class TrackInfo:
    artist: str
    track: str
    album: Optional[str]
    duration_s: Optional[int]
    timestamp: int  # Unix seconds


@dataclass
class UserCreds:
    service: str   # "lastfm" | "listenbrainz"
    username: str
    secret: str    # session key for Last.fm; user token for ListenBrainz


@runtime_checkable
class Scrobbler(Protocol):
    service_name: str

    async def now_playing(self, creds: UserCreds, track: TrackInfo) -> None: ...
    async def scrobble(self, creds: UserCreds, track: TrackInfo) -> None: ...
    async def love(self, creds: UserCreds, track: TrackInfo) -> None: ...

    async def start_linking(self, matrix_user_id: str) -> str:
        """Returns the auth URL to give the user."""

    async def finalize_linking(self, matrix_user_id: str, request_token: str) -> UserCreds:
        """Called by !fm confirm. Raises on failure."""
