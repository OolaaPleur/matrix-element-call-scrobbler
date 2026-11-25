import logging

from nio import MatrixRoom

logger = logging.getLogger(__name__)

EVT_TRACK_STARTED  = "dev.elementcall.musicbot.track_started"
EVT_TRACK_FINISHED = "dev.elementcall.musicbot.track_finished"


def event_type(event) -> str | None:
    return getattr(event, "type", None) or getattr(event, "source", {}).get("type")


class EventHandler:
    def __init__(self, play_tracker, bot_user_id: str):
        self._tracker = play_tracker
        self._bot_user_id = bot_user_id

    async def on_room_event(self, room: MatrixRoom, event) -> None:
        if event.sender == self._bot_user_id:
            return

        evt_type = event_type(event)
        if evt_type not in (EVT_TRACK_STARTED, EVT_TRACK_FINISHED):
            return
        content = getattr(event, "source", {}).get("content", {})
        logger.info("Received %s play_id=%s room=%s sender=%s",
                    evt_type.rsplit(".", 1)[-1], content.get("play_id"), room.room_id, event.sender)
        if evt_type == EVT_TRACK_STARTED:
            await self._tracker.on_track_started(content, room.room_id, event.sender)
        else:
            await self._tracker.on_track_finished(content, room.room_id, event.sender)
