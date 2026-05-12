import logging

from linking import LinkingManager
from storage import Storage
from scrobblers.lastfm import LastFmScrobbler
from scrobblers.base import TrackInfo

logger = logging.getLogger(__name__)

HELP_TEXT = """\
**!fm commands**
- `!fm link` — link your Last.fm account
- `!fm confirm` — complete linking after approving on Last.fm
- `!fm unlink` — unlink your account
- `!fm on` / `!fm off` — enable or pause scrobbling
- `!fm status` — show link status and settings
- `!fm love` — love the last scrobbled track on Last.fm
- `!fm ignore here` — stop scrobbling in this room
- `!fm unignore here` — resume scrobbling in this room
- `!fm help` — show this list

For read-only stats (!fm np, !fm recent, !fm top) see https://github.com/zerw0/fmatrix\
"""


class CommandRouter:
    def __init__(self, storage: Storage, linker: LinkingManager, scrobbler: LastFmScrobbler):
        self._storage = storage
        self._linker = linker
        self._scrobbler = scrobbler

    async def handle(self, sender: str, room_id: str, text: str) -> str | None:
        text = text.strip()
        if not text.startswith("!fm"):
            return None
        parts = text.split()
        if len(parts) < 2:
            return HELP_TEXT

        sub = parts[1].lower()

        if sub == "link":
            url = await self._linker.cmd_link(sender)
            return f"Authorize scrobbling here:\n{url}\n\nThen run `!fm confirm`."

        if sub == "confirm":
            return await self._linker.cmd_confirm(sender)

        if sub == "unlink":
            return await self._linker.cmd_unlink(sender)

        if sub == "on":
            return await self._linker.cmd_on(sender)

        if sub == "off":
            return await self._linker.cmd_off(sender)

        if sub == "status":
            return await self._linker.cmd_status(sender)

        if sub == "love":
            return await self._cmd_love(sender)

        if sub == "ignore" and len(parts) >= 3 and parts[2].lower() == "here":
            return await self._linker.cmd_ignore_here(sender, room_id)

        if sub == "unignore" and len(parts) >= 3 and parts[2].lower() == "here":
            return await self._linker.cmd_unignore_here(sender, room_id)

        if sub == "help":
            return HELP_TEXT

        return f"Unknown command `{sub}`. Try `!fm help`."

    async def _cmd_love(self, sender: str) -> str:
        creds = await self._storage.get_creds(sender)
        if not creds:
            return "Not linked to Last.fm. Use `!fm link` first."
        recent = await self._storage.get_recent_scrobble(sender, within_seconds=3600)
        if not recent:
            return "No track scrobbled in the past hour."
        ti = TrackInfo(
            artist=recent["artist"],
            track=recent["track"],
            album=None,
            duration_s=None,
            timestamp=recent["scrobbled_at"],
        )
        try:
            await self._scrobbler.love(creds, ti)
        except Exception as exc:
            logger.warning("love failed for %s: %s", sender, exc)
            return f"Failed to love track: {exc}"
        return f"Loved **{ti.artist} — {ti.track}** on Last.fm."
