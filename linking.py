import logging
import time

from scrobblers.lastfm import LastFmScrobbler
from storage import Storage

logger = logging.getLogger(__name__)


class LinkingManager:
    def __init__(self, storage: Storage, scrobbler: LastFmScrobbler):
        self._storage = storage
        self._scrobbler = scrobbler

    async def cmd_link(self, matrix_user_id: str) -> str:
        token, url = await self._scrobbler.start_linking(matrix_user_id)
        await self._storage.save_pending_link(matrix_user_id, token)
        return url

    async def cmd_confirm(self, matrix_user_id: str) -> str:
        token = await self._storage.pop_pending_link(matrix_user_id)
        if not token:
            return "No pending link found. Run `!fm link` first."
        try:
            creds = await self._scrobbler.finalize_linking(token)
        except Exception as exc:
            logger.warning("finalize_linking failed for %s: %s", matrix_user_id, exc)
            return f"Linking failed: {exc}. Make sure you approved the request on Last.fm, then try `!fm confirm` again."
        await self._storage.save_linked_account(matrix_user_id, creds)
        logger.info("Linked %s to Last.fm as %s", matrix_user_id, creds.username)
        return f"Linked to Last.fm as **{creds.username}**. Scrobbling is now active."

    async def cmd_unlink(self, matrix_user_id: str) -> str:
        info = await self._storage.get_linked_info(matrix_user_id)
        if not info:
            return "No Last.fm account linked."
        await self._storage.delete_linked_account(matrix_user_id)
        return "Unlinked from Last.fm."

    async def cmd_on(self, matrix_user_id: str) -> str:
        info = await self._storage.get_linked_info(matrix_user_id)
        if not info:
            return "No Last.fm account linked. Run `!fm link` first."
        await self._storage.set_enabled(matrix_user_id, True)
        return f"Scrobbling enabled for **{info['username']}**."

    async def cmd_off(self, matrix_user_id: str) -> str:
        info = await self._storage.get_linked_info(matrix_user_id)
        if not info:
            return "No Last.fm account linked."
        await self._storage.set_enabled(matrix_user_id, False)
        return f"Scrobbling paused for **{info['username']}**."

    async def cmd_status(self, matrix_user_id: str) -> str:
        info = await self._storage.get_linked_info(matrix_user_id)
        if not info:
            return "Not linked to Last.fm. Use `!fm link` to link your account."
        blacklisted = await self._storage.get_blacklisted_rooms(matrix_user_id)
        lines = [
            f"Last.fm: **{info['username']}**",
            f"Scrobbling: {'on' if info['enabled'] else 'off (use `!fm on` to re-enable)'}",
            f"Linked at: <t:{info['linked_at']}:f>",
        ]
        if blacklisted:
            lines.append(f"Ignored rooms: {len(blacklisted)} room(s)")
        return "\n".join(lines)

    async def cmd_ignore_here(self, matrix_user_id: str, room_id: str) -> str:
        await self._storage.add_room_blacklist(matrix_user_id, room_id)
        return "Scrobbling disabled for this room. Use `!fm unignore here` to re-enable."

    async def cmd_unignore_here(self, matrix_user_id: str, room_id: str) -> str:
        await self._storage.remove_room_blacklist(matrix_user_id, room_id)
        return "Scrobbling re-enabled for this room."
