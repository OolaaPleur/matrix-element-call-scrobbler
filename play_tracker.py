import asyncio
import logging
import time
from typing import Optional

from config import Config
from scrobblers.base import TrackInfo, UserCreds
from scrobblers.lastfm import ERR_RATE_LIMIT, LastFmError, ScrobbleIgnored
from storage import Storage

logger = logging.getLogger(__name__)


def _emitter_allowed(sender: str, cfg: Config) -> bool:
    # The event's real sender, not content["emitter"]: anyone in the room could write that.
    return cfg.emitters_allowed_user_ids == ["*"] or sender in cfg.emitters_allowed_user_ids


def _should_accept_track(event_content: dict, sender: str, cfg: Config) -> tuple[bool, str | None]:
    source  = event_content.get("source", "")
    quality = event_content.get("metadata_quality", "low")
    artist  = (event_content.get("artist") or "").strip()
    track   = (event_content.get("track") or "").strip()

    if not _emitter_allowed(sender, cfg):
        return False, f"emitter not allowed: {sender}"

    if cfg.sources_allowed != ["*"]:
        if source not in cfg.sources_allowed:
            return False, f"source not in allowed: {source}"
    if source in cfg.sources_denied:
        return False, f"source in denied: {source}"

    if cfg.quality_require_high and quality != "high":
        return False, f"metadata_quality={quality}, require_high=true"

    if not artist or not track:
        return False, "empty artist or track"

    return True, None


def _meets_lastfm_threshold(played_s: int, duration_s: int) -> bool:
    return played_s >= 30 and (played_s >= duration_s / 2 or played_s >= 240)


class PlayTracker:
    def __init__(self, storage: Storage, scrobbler, send_message_fn, cfg: Config, grace_seconds: int = 60):
        self._storage = storage
        self._scrobbler = scrobbler
        self._send_message = send_message_fn  # async fn(room_id, text)
        self._cfg = cfg
        self._grace_seconds = grace_seconds
        self._drain_task: Optional[asyncio.Task] = None
        self._sweep_task: Optional[asyncio.Task] = None
        self._now_playing_tasks: set[asyncio.Task] = set()  # keep references until done

    def start_background_tasks(self, drain_interval: int, sweep_interval: int):
        self._drain_task = asyncio.create_task(self._drain_loop(drain_interval))
        self._sweep_task = asyncio.create_task(self._sweep_loop(sweep_interval))

    async def stop(self):
        for task in (self._drain_task, self._sweep_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    # ── track_started handler ────────────────────────────────────────────────

    async def on_track_started(self, event_content: dict, room_id: str, sender: str):
        accept, drop_reason = _should_accept_track(event_content, sender, self._cfg)
        if accept and not event_content.get("play_id"):
            accept, drop_reason = False, "no play_id"
        if accept and await self._storage.is_play_finished(event_content["play_id"]):
            logger.debug("track_started play_id=%s already finished (replay), ignored", event_content["play_id"])
            return
        if not accept:
            logger.info(
                "track_started dropped: play_id=%s reason=%s artist=%r track=%r source=%r quality=%r",
                event_content.get("play_id"), drop_reason,
                event_content.get("artist"), event_content.get("track"),
                event_content.get("source"), event_content.get("metadata_quality"),
            )
            return

        play_id    = event_content["play_id"]
        artist     = event_content["artist"]
        track      = event_content["track"]
        album      = event_content.get("album") or None
        duration_s = int(event_content.get("duration_s") or 0)
        started_at = int(event_content.get("started_at") or time.time())
        participants = event_content.get("call_participants", [])
        logger.debug("play_id=%s participants=%s", play_id, participants)
        # Events replayed after a restart can be long over: track them (their
        # track_finished may follow), but don't announce them as playing now.
        still_playing = time.time() - started_at < max(duration_s, 60)

        inserted = 0
        for user_id in participants:
            creds = await self._get_eligible_creds(user_id, room_id)
            if creds is None:
                logger.debug("play_id=%s skipped user=%s (no creds or blacklisted)", play_id, user_id)
                continue

            await self._storage.insert_play_state(
                play_id, user_id, room_id, started_at, artist, track, album, duration_s
            )
            logger.info("play_id=%s RECEIVED_STARTED for user=%s", play_id, user_id)
            inserted += 1

            if still_playing:
                ti = TrackInfo(artist=artist, track=track, album=album,
                               duration_s=duration_s, timestamp=started_at)
                task = asyncio.create_task(self._send_now_playing(creds, ti))
                self._now_playing_tasks.add(task)
                task.add_done_callback(self._now_playing_tasks.discard)

        if inserted == 0:
            logger.warning(
                "play_id=%s track_started produced no play_state rows (participants=%d, none linked/eligible)",
                play_id, len(participants),
            )

    async def _send_now_playing(self, creds: UserCreds, track: TrackInfo):
        try:
            await self._scrobbler.now_playing(creds, track)
        except Exception:
            logger.debug("now_playing failed (silenced)", exc_info=True)

    # ── track_finished handler ───────────────────────────────────────────────

    async def on_track_finished(self, event_content: dict, room_id: str, sender: str):
        play_id = event_content.get("play_id")
        if not _emitter_allowed(sender, self._cfg):
            logger.info("track_finished dropped: play_id=%s emitter not allowed: %s", play_id, sender)
            return
        if not play_id:
            logger.info("track_finished dropped: no play_id")
            return
        if await self._storage.is_play_finished(play_id):
            logger.debug("track_finished play_id=%s already handled (replay), ignored", play_id)
            return
        played_s   = int(event_content.get("played_s") or 0)
        reason     = event_content.get("reason", "")
        eligible   = set(event_content.get("eligible_participants", []))

        rows = [r for r in await self._storage.get_play_state_rows(play_id) if r["room_id"] == room_id]
        if not rows:
            logger.debug("play_id=%s no play_state rows found (never started for a linked user)", play_id)

        scrobbles = []
        for row in rows:
            user_id    = row["matrix_user_id"]
            duration_s = row["duration_s"]
            artist     = row["artist"]
            track      = row["track"]

            if (user_id in eligible
                    and reason == "finished"
                    and _meets_lastfm_threshold(played_s, duration_s)):
                scrobbles.append({
                    "matrix_user_id": user_id,
                    "service": "lastfm",
                    "artist": artist,
                    "track": track,
                    "album": row.get("album"),
                    "duration_s": duration_s,
                    "played_at": row["started_at"],
                    "room_id": row["room_id"],
                })
                logger.info("play_id=%s ENQUEUED_SCROBBLE for user=%s track='%s - %s'",
                            play_id, user_id, artist, track)
            else:
                logger.info(
                    "play_id=%s DROPPED for user=%s (eligible=%s reason=%s played_s=%d duration_s=%d)",
                    play_id, user_id, user_id in eligible, reason, played_s, duration_s,
                )

        # Queue, clear the play and mark it finished in one transaction (replay-safe).
        await self._storage.finish_play(play_id, scrobbles)

    # ── Queue drain ──────────────────────────────────────────────────────────

    async def _drain_loop(self, interval: int):
        while True:
            try:
                await self._drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Drain loop error")
            await asyncio.sleep(interval)

    async def _drain_once(self):
        rows = await self._storage.get_due_scrobbles()
        for row in rows:
            row_id    = row["id"]
            user_id   = row["matrix_user_id"]
            service   = row["service"]
            attempts  = row["attempts"]

            creds = await self._storage.get_creds(user_id, service)
            if creds is None:
                logger.warning("Scrobble id=%d: user=%s has no valid creds, dropping", row_id, user_id)
                await self._storage.delete_scrobble_queue_row(row_id)
                continue

            ti = TrackInfo(
                artist=row["artist"],
                track=row["track"],
                album=row.get("album"),
                duration_s=row.get("duration_s"),
                timestamp=row["played_at"],
            )

            try:
                await self._scrobbler.scrobble(creds, ti)
            except ScrobbleIgnored as exc:
                if exc.retryable:  # daily scrobble limit
                    logger.warning("Scrobble id=%d attempt=%d deferred: %s", row_id, attempts, exc)
                    await self._storage.bump_scrobble_retry(row_id, attempts, str(exc))
                else:
                    logger.warning("Scrobble id=%d user=%s dropped: %s", row_id, user_id, exc)
                    await self._storage.delete_scrobble_queue_row(row_id)
            except LastFmError as exc:
                if exc.invalid_session:
                    await self._unlink_expired(row, exc)
                elif exc.retryable:
                    logger.warning("Scrobble id=%d attempt=%d failed: %s", row_id, attempts, exc)
                    await self._storage.bump_scrobble_retry(row_id, attempts, str(exc))
                    if exc.code == ERR_RATE_LIMIT:
                        logger.warning("Last.fm rate limit: stopping this drain pass")
                        return
                else:
                    # Invalid parameters / resource: a retry won't help.
                    logger.error("Scrobble id=%d user=%s dropped: %s", row_id, user_id, exc)
                    await self._storage.delete_scrobble_queue_row(row_id)
            except Exception as exc:  # network errors, Last.fm outages without a JSON body
                logger.warning("Scrobble id=%d attempt=%d failed: %s", row_id, attempts, exc)
                await self._storage.bump_scrobble_retry(row_id, attempts, str(exc))
            else:
                await self._storage.complete_scrobble(row_id, user_id, ti.artist, ti.track, ti.timestamp)
                logger.info("SCROBBLED id=%d user=%s '%s - %s'", row_id, user_id, ti.artist, ti.track)

    async def _unlink_expired(self, row: dict, exc: Exception):
        # Unlink rather than disable: `!fm on` would just re-enable the dead session key.
        # The user's other queued scrobbles are dropped as "no valid creds" on their turn.
        user_id, service = row["matrix_user_id"], row["service"]
        logger.error("Bad session for user=%s, unlinking. Error: %s", user_id, exc)
        await self._storage.delete_linked_account(user_id, service)
        await self._storage.delete_scrobble_queue_row(row["id"])
        if row.get("room_id"):
            await self._send_message(
                row["room_id"],
                f"{user_id}: your Last.fm session expired. Run `!fm link` to re-link."
            )

    # ── Abandoned sweep ──────────────────────────────────────────────────────

    async def _sweep_loop(self, interval: int):
        while True:
            try:
                count = await self._storage.sweep_abandoned_plays(
                    grace_seconds=self._grace_seconds
                )
                if count:
                    logger.warning("Swept %d abandoned play_state rows", count)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Sweep loop error")
            await asyncio.sleep(interval)

    # ── Helpers ──────────────────────────────────────────────────────────────

    async def _get_eligible_creds(self, user_id: str, room_id: str) -> Optional[UserCreds]:
        creds = await self._storage.get_creds(user_id, "lastfm")
        if creds is None:
            return None
        if await self._storage.is_room_blacklisted(user_id, room_id):
            return None
        return creds
