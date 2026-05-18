import logging

from nio import MatrixRoom

logger = logging.getLogger(__name__)

EVT_TRACK_STARTED  = "dev.elementcall.musicbot.track_started"
EVT_TRACK_FINISHED = "dev.elementcall.musicbot.track_finished"


class EventHandler:
    def __init__(self, play_tracker, command_router, bot_user_id: str):
        self._tracker = play_tracker
        self._commands = command_router
        self._bot_user_id = bot_user_id

    async def on_room_event(self, room: MatrixRoom, event) -> None:
        if event.sender == self._bot_user_id:
            return

        evt_type = getattr(event, "type", None) or getattr(event, "source", {}).get("type")

        if evt_type == EVT_TRACK_STARTED:
            content = getattr(event, "source", {}).get("content", {})
            logger.info("Received track_started play_id=%s room=%s", content.get("play_id"), room.room_id)
            await self._tracker.on_track_started(content, room.room_id)

        elif evt_type == EVT_TRACK_FINISHED:
            content = getattr(event, "source", {}).get("content", {})
            logger.info("Received track_finished play_id=%s room=%s", content.get("play_id"), room.room_id)
            await self._tracker.on_track_finished(content, room.room_id)

    async def on_room_message(self, room: MatrixRoom, event) -> None:
        if event.sender == self._bot_user_id:
            return
        body = getattr(event, "body", "") or ""
        if not body.startswith("!fm"):
            return
        reply = await self._commands.handle(event.sender, room.room_id, body)
        if reply:
            await self._send_reply(room.room_id, reply)

    # send_reply is wired up by bot.py after init
    async def _send_reply(self, room_id: str, text: str):
        pass
