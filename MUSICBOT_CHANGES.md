# Music Bot Modifications

Changes required in the existing music bot (`matrix-element-call-musicbot/bot.py`).

## 1. Event constants and imports — add at top of bot.py

```python
import re
import uuid
from urllib.parse import urlparse

EVT_TRACK_STARTED  = "dev.oolaa.musicbot.track_started"
EVT_TRACK_FINISHED = "dev.oolaa.musicbot.track_finished"
BOT_KIND = "matrix-element-call-musicbot"
```

## 2. Source detection — add as module-level function

```python
def detect_source(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if host == "music.youtube.com":
        return "youtube_music"
    if host in {"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"}:
        return "youtube"
    return "other"
```

## 3. Metadata extraction — add as module-level helpers

The bot always sends artist/track/album/quality. yt-dlp fields are used when available; otherwise the title is parsed heuristically. Even events with empty artist/track are emitted — the scrobbler's sanity check drops them.

```python
_NOISE_RE = re.compile(
    r"\s*[\(\[][^)\]]*"
    r"(?:official|video|audio|lyrics?|hd|4k|"
    r"remaster(?:ed)?|live|music\s*video|mv|"
    r"visualizer|explicit|clean|extended)"
    r"[^)\]]*[\)\]]\s*",
    re.IGNORECASE,
)
_TOPIC_SUFFIX_RE = re.compile(r"\s*-\s*Topic\s*$", re.IGNORECASE)


def _clean_title(title: str) -> str:
    """Strip common YouTube title noise like '(Official Video)', '[HD]', etc."""
    prev = None
    cur = title
    while prev != cur:
        prev = cur
        cur = _NOISE_RE.sub(" ", cur).strip()
    cur = _TOPIC_SUFFIX_RE.sub("", cur).strip()
    return cur.strip(" -")


def extract_metadata(yt_info: dict) -> tuple[str, str, str, str]:
    """
    Returns (artist, track, album, quality) where quality is "high" or "low".
    Strategy order:
      1. yt-dlp populated artist/track fields directly  -> "high"
      2. " - " split on title with noise cleanup        -> "low"
      3. channel + cleaned title                        -> "low"
      4. all empty                                      -> "low" (scrobbler will drop)
    """
    yt_artist = (yt_info.get("artist") or "").strip()
    yt_track  = (yt_info.get("track")  or "").strip()
    yt_album  = (yt_info.get("album")  or "").strip()
    if yt_artist and yt_track:
        return yt_artist, yt_track, yt_album, "high"

    title = (yt_info.get("title") or "").strip()
    if " - " in title:
        left, right = title.split(" - ", 1)
        artist = _TOPIC_SUFFIX_RE.sub("", left).strip()
        track = _clean_title(right)
        if artist and track:
            return artist, track, yt_album, "low"

    channel = (yt_info.get("channel") or yt_info.get("uploader") or "").strip()
    cleaned = _clean_title(title)
    if channel and cleaned:
        channel = _TOPIC_SUFFIX_RE.sub("", channel).strip()
        return channel, cleaned, yt_album, "low"

    return "", "", "", "low"
```

### Metadata quality examples

| URL kind | yt-dlp `artist`/`track` | Title | Result | Quality |
|---|---|---|---|---|
| YT Music topic channel | "Rick Astley" / "Never Gonna Give You Up" | (n/a) | Rick Astley / Never Gonna Give You Up | high |
| VEVO upload | sometimes populated | "Rick Astley - Never Gonna Give You Up (Official Music Video)" | from fields if present, else split | high or low |
| Standard "Artist - Track [HD]" upload | empty | "Daft Punk - Around the World (HD)" | Daft Punk / Around the World | low |
| Plain channel upload | empty | "live session at the studio" | uploader / "live session at the studio" | low |
| DJ set with " - " in track name | empty | "Some DJ - Track Name - Live Mix (Extended)" | Some DJ / Track Name - Live Mix | low |

## 4. Call participant tracking — add to IntegratedBot.__init__

```python
self._call_participants: dict[str, set[str]] = {}  # room_id → set of user_ids
```

Then in the sync callback or wherever state events arrive, parse
`org.matrix.msc3401.call.member` state events:

```python
async def _on_call_member_state(self, room: MatrixRoom, event):
    content = event.source.get("content", {})
    user_id = event.state_key
    room_id = room.room_id
    memberships = content.get("memberships", [])
    active = any(m.get("application") == "m.call" for m in memberships)
    if active:
        self._call_participants.setdefault(room_id, set()).add(user_id)
    else:
        self._call_participants.get(room_id, set()).discard(user_id)

def get_call_participants(self, room_id: str) -> list[str]:
    participants = self._call_participants.get(room_id, set())
    return [u for u in participants if u != self.client.user_id]
```

Register the callback:
```python
self.client.add_event_callback(self._on_call_member_state, UnknownEvent)
# filter for org.matrix.msc3401.call.member inside the handler
```

## 5. Active play state — add to IntegratedBot

```python
self._active_play: dict | None = None   # per-room; if you support multiple rooms, key by room_id
```

## 6. Emit track_started — in _advance_queue / wherever "▶️ Now playing:" is sent

Insert immediately before or after the "▶️" message:

```python
artist, track_name, album, quality = extract_metadata(yt_info)
source = detect_source(yt_info.get("url", ""))
play_id = str(uuid.uuid4())
participants = self.get_call_participants(call_room_id)
started_at = int(time.time())

self._active_play = {
    "play_id": play_id,
    "started_at": started_at,
    "started_participants": set(participants),
    "duration_s": int(yt_info.get("duration") or 0),
    "finished": False,
}

await self.client.room_send(
    room_id=call_room_id,
    message_type=EVT_TRACK_STARTED,
    content={
        "play_id": play_id,
        "source": source,
        "source_url": yt_info.get("url", ""),
        "artist": artist,
        "track": track_name,
        "album": album,
        "duration_s": int(yt_info.get("duration") or 0),
        "metadata_quality": quality,
        "started_at": started_at,
        "call_participants": participants,
        "emitter": {
            "user_id": self.client.user_id,
            "kind": BOT_KIND,
        },
    },
    ignore_unverified_devices=False,
)
```

Note: always emit the event, even when `artist`/`track` are empty. The scrobbler's sanity check drops those events. Keeping the stream complete lets non-scrobbler consumers (logs, history) see every play.

### Why no `scrobblable` field

The old design had the music bot decide what is scrobblable (`source == "youtube_music" AND artist AND track`). This conflated transport description with scrobble policy. The revised design separates them: the music bot describes what's playing via `source`, `metadata_quality`, and `emitter`; the scrobbler bot decides what to act on via its `[sources]`, `[quality]`, and `[emitters]` config. The same event stream can later feed non-scrobbler consumers without re-teaching the music bot about each consumer's policy.

## 7. Emit track_finished — helper method

Add this method to IntegratedBot and call it from every termination path:

```python
async def _emit_track_finished(self, call_room_id: str, reason: str, played_s: int):
    if self._active_play is None or self._active_play.get("finished"):
        return
    self._active_play["finished"] = True  # guard against double-emit

    current = set(self.get_call_participants(call_room_id))
    eligible = list(self._active_play["started_participants"] & current)

    await self.client.room_send(
        room_id=call_room_id,
        message_type=EVT_TRACK_FINISHED,
        content={
            "play_id": self._active_play["play_id"],
            "played_s": int(played_s),
            "reason": reason,
            "finished_at": int(time.time()),
            "eligible_participants": eligible,
        },
        ignore_unverified_devices=False,
    )
    self._active_play = None
```

## 8. Call _emit_track_finished from every termination path

| Code path | reason |
|---|---|
| EOF / `play_ended` from call_worker | `"finished"` |
| `!skip` / `!next` command | `"skipped"` |
| `!stop` command | `"stopped"` |
| Call members → 0 (bot leaves call) | `"stopped"` |
| yt-dlp or decoder error | `"error"` |

For `played_s`: if call_worker reports duration played, use that.
Otherwise use `int(time.time()) - self._active_play["started_at"]` as fallback.

Example in the `_wait_for_worker_playback` path:
```python
event = await self.call_worker.wait_for_playback_terminal()
event_name = event.get("event")
played_s = event.get("played_s") or int(time.time() - self._active_play["started_at"])

if event_name == "play_ended":
    await self._emit_track_finished(call_room_id, "finished", played_s)
elif event_name in ("play_stopped",):
    await self._emit_track_finished(call_room_id, self._pending_stop_reason or "stopped", played_s)
```

## 9. yt-dlp metadata — ensure these fields are extracted

When fetching track info, make sure to capture:
```python
info = {
    "artist":   ydl_info.get("artist")   or "",
    "track":    ydl_info.get("track")    or "",
    "album":    ydl_info.get("album")    or "",
    "title":    ydl_info.get("title")    or "",
    "channel":  ydl_info.get("channel")  or ydl_info.get("uploader") or "",
    "duration": ydl_info.get("duration") or 0,
    "url": original_url,
}
```
All four primary fields (`artist`, `track`, `album`, `duration`) are top-level in yt-dlp's info dict for `music.youtube.com` URLs. `title` and `channel` are the fallback inputs for the heuristic extractor.
