import logging

from nio import AsyncClient, InviteMemberEvent, MatrixRoom

logger = logging.getLogger(__name__)


class AutoAccept:
    def __init__(self, client: AsyncClient, trusted_users: list[str]):
        self._client = client
        self._trusted = set(trusted_users)

    async def on_invite(self, room: MatrixRoom, event: InviteMemberEvent):
        if event.membership != "invite":
            return
        if event.sender in self._trusted:
            logger.info("Auto-accepting invite from trusted user %s to %s", event.sender, room.room_id)
            await self._join(room.room_id)

    async def _join(self, room_id: str):
        try:
            await self._client.join(room_id)
        except Exception:
            logger.exception("Failed to join %s", room_id)
