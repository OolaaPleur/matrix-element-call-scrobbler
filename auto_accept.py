import asyncio
import logging
import time

from nio import AsyncClient, InviteMemberEvent, MatrixRoom

logger = logging.getLogger(__name__)

_DM_ACCEPTS_PER_MINUTE = 5
_DM_MAX_OPEN_ROOMS = 100


class AutoAccept:
    def __init__(self, client: AsyncClient, trusted_users: list[str],
                 auto_accept_dm: bool, dm_rate_limit: bool = True):
        self._client = client
        self._trusted = set(trusted_users)
        self._auto_accept_dm = auto_accept_dm
        self._dm_rate_limit = dm_rate_limit
        self._dm_accepts_this_minute: list[float] = []
        self._open_dm_count = 0

    async def on_invite(self, room: MatrixRoom, event: InviteMemberEvent):
        if event.membership != "invite":
            return
        room_id = room.room_id
        sender = event.sender

        if sender in self._trusted:
            logger.info("Auto-accepting invite from trusted user %s to %s", sender, room_id)
            await self._join(room_id)
            return

        if self._auto_accept_dm and self._looks_like_dm(room):
            if not self._dm_rate_ok():
                logger.warning("DM rate limit hit, ignoring invite from %s", sender)
                return
            if self._open_dm_count >= _DM_MAX_OPEN_ROOMS:
                logger.warning("DM room cap reached, ignoring invite from %s", sender)
                return
            logger.info("Auto-accepting DM invite from %s to %s", sender, room_id)
            await self._join(room_id)
            self._open_dm_count += 1

    def _looks_like_dm(self, room: MatrixRoom) -> bool:
        return getattr(room, "member_count", 99) <= 2

    def _dm_rate_ok(self) -> bool:
        now = time.monotonic()
        self._dm_accepts_this_minute = [t for t in self._dm_accepts_this_minute if now - t < 60]
        if len(self._dm_accepts_this_minute) >= _DM_ACCEPTS_PER_MINUTE:
            return False
        self._dm_accepts_this_minute.append(now)
        return True

    async def _join(self, room_id: str):
        try:
            await self._client.join(room_id)
        except Exception:
            logger.exception("Failed to join %s", room_id)
