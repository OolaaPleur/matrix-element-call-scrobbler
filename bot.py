import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    KeyVerificationCancel,
    KeyVerificationKey,
    KeyVerificationMac,
    KeyVerificationStart,
    LocalProtocolError,
    MatrixRoom,
    RoomEncryptedEvent,
    RoomMessageText,
    SasVerification,
    UnknownEvent,
)

from auto_accept import AutoAccept
from commands import CommandRouter
from config import Config
from event_handler import EventHandler
from linking import LinkingManager
from play_tracker import PlayTracker
from scrobblers.lastfm import LastFmScrobbler
from storage import Storage

logger = logging.getLogger(__name__)

DEVICE_ID_FILE = "data/device_id"
CRYPTO_STORE_PATH = "data/crypto_store"


class ScrobblerBot:
    def __init__(self, config: Config):
        self._config = config
        self._storage = Storage(config.db_path, config.encryption_key)
        self._scrobbler = LastFmScrobbler(config.lastfm_api_key, config.lastfm_shared_secret)
        self._linker = LinkingManager(self._storage, self._scrobbler)
        self._commands = CommandRouter(self._storage, self._linker, self._scrobbler)
        self._play_tracker = PlayTracker(
            storage=self._storage,
            scrobbler=self._scrobbler,
            send_message_fn=self._send_message,
            cfg=config,
            grace_seconds=config.abandoned_play_grace_seconds,
        )
        self._client: Optional[AsyncClient] = None
        self._event_handler: Optional[EventHandler] = None
        self._auto_accept: Optional[AutoAccept] = None

    # ── Setup ────────────────────────────────────────────────────────────────

    async def _setup_client(self):
        Path(CRYPTO_STORE_PATH).mkdir(parents=True, exist_ok=True)
        device_id = None
        if Path(DEVICE_ID_FILE).exists():
            device_id = Path(DEVICE_ID_FILE).read_text().strip() or None

        cfg = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=True,
        )
        self._client = AsyncClient(
            self._config.homeserver,
            self._config.user_id,
            store_path=CRYPTO_STORE_PATH,
            config=cfg,
            device_id=device_id,
        )

    async def _setup_e2ee(self):
        c = self._client
        # whoami
        try:
            whoami = await c.whoami()
            logger.info("Logged in as %s device=%s", whoami.user_id, whoami.device_id)
            Path(DEVICE_ID_FILE).write_text(c.device_id or "")
        except LocalProtocolError:
            pass

        # restore_login / upload keys
        try:
            await c.keys_upload()
        except LocalProtocolError:
            logger.debug("keys_upload: nothing to upload")

        try:
            await c.keys_query()
        except LocalProtocolError:
            logger.debug("keys_query: no keys to query")

    async def _login(self):
        c = self._config
        # Attempt token-based restore first
        token_file = Path("data/access_token")
        if token_file.exists():
            token = token_file.read_text().strip()
            if token:
                self._client.access_token = token
                self._client.user_id = c.user_id
                logger.info("Restored session from token file")
                return

        resp = await self._client.login(c.password, device_name=c.device_name)
        if hasattr(resp, "access_token"):
            token_file.write_text(resp.access_token)
            logger.info("Logged in, token saved")
        else:
            raise RuntimeError(f"Login failed: {resp}")

    # ── Verification ─────────────────────────────────────────────────────────

    def _register_verification_callbacks(self):
        c = self._client
        c.add_to_device_callback(self._on_verification_start, KeyVerificationStart)
        c.add_to_device_callback(self._on_verification_key, KeyVerificationKey)
        c.add_to_device_callback(self._on_verification_mac, KeyVerificationMac)
        c.add_to_device_callback(self._on_verification_cancel, KeyVerificationCancel)

    async def _on_verification_start(self, event: KeyVerificationStart):
        if event.sender not in self._config.auto_accept_users:
            logger.info("Ignoring verification from non-trusted %s", event.sender)
            return
        logger.info("Auto-accepting SAS verification from %s", event.sender)
        try:
            sas = self._client.key_verifications.get(event.transaction_id)
            if sas is None:
                sas = await self._client.accept_key_verification(event.transaction_id)
            await self._client.to_device(sas.share_key())
        except Exception:
            logger.exception("Verification start error")

    async def _on_verification_key(self, event: KeyVerificationKey):
        sas: Optional[SasVerification] = self._client.key_verifications.get(event.transaction_id)
        if sas is None:
            return
        if event.sender not in self._config.auto_accept_users:
            return
        try:
            await self._client.to_device(sas.accept_sas())
            await self._client.to_device(sas.confirm_sas())
        except Exception:
            logger.exception("Verification key error")

    async def _on_verification_mac(self, event: KeyVerificationMac):
        sas = self._client.key_verifications.get(event.transaction_id)
        if sas is None:
            return
        try:
            await self._client.to_device(sas.get_mac())
        except Exception:
            logger.exception("Verification mac error")

    async def _on_verification_cancel(self, event: KeyVerificationCancel):
        logger.info("Verification cancelled by %s: %s", event.sender, event.reason)

    # ── Message sending ──────────────────────────────────────────────────────

    async def _send_message(self, room_id: str, text: str):
        try:
            await self._client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": text},
                ignore_unverified_devices=False,
            )
        except Exception:
            logger.exception("Failed to send message to %s", room_id)

    # ── Event callbacks ──────────────────────────────────────────────────────

    def _register_event_callbacks(self):
        c = self._client
        c.add_event_callback(self._on_invite, InviteMemberEvent)
        c.add_event_callback(self._on_room_message, (RoomMessageText,))
        c.add_event_callback(self._on_unknown_event, (UnknownEvent,))
        # Decrypted events arrive as their decrypted type; catch the raw encrypted
        # ones too so we can log undecryptable messages
        c.add_event_callback(self._on_encrypted_event, (RoomEncryptedEvent,))

    async def _on_invite(self, room: MatrixRoom, event: InviteMemberEvent):
        if event.state_key == self._config.user_id:
            await self._auto_accept.on_invite(room, event)

    async def _on_room_message(self, room: MatrixRoom, event: RoomMessageText):
        if event.sender == self._config.user_id:
            return
        body = getattr(event, "body", "") or ""
        if not body.startswith("!fm"):
            return
        reply = await self._commands.handle(event.sender, room.room_id, body)
        if reply:
            await self._send_message(room.room_id, reply)

    async def _on_unknown_event(self, room: MatrixRoom, event: UnknownEvent):
        await self._event_handler.on_room_event(room, event)

    async def _on_encrypted_event(self, room: MatrixRoom, event: RoomEncryptedEvent):
        # Undecryptable — log only, matrix-nio will handle decrypted form separately
        logger.debug("Received encrypted event from %s in %s (may decrypt later)", event.sender, room.room_id)

    # ── Restart-safety replay ────────────────────────────────────────────────

    async def _replay_recent_timeline(self):
        lookback = self._config.recovery_lookback_hours * 3600
        await self._storage.drop_old_play_states(lookback)

        # Walk joined rooms; for each, paginate recent events looking for track_finished
        # whose play_id is still in play_state (meaning scrobbler missed the event)
        for room_id, room in self._client.rooms.items():
            try:
                resp = await self._client.room_messages(room_id, start="", limit=500)
                for event in getattr(resp, "chunk", []):
                    evt_type = getattr(event, "type", None) or getattr(event, "source", {}).get("type")
                    if evt_type == "dev.oolaa.musicbot.track_finished":
                        content = getattr(event, "source", {}).get("content", {})
                        play_id = content.get("play_id")
                        if play_id:
                            rows = await self._storage.get_play_state_rows(play_id)
                            if rows:
                                logger.info("Replay: found pending play_id=%s, processing", play_id)
                                await self._play_tracker.on_track_finished(content, room_id)
            except Exception:
                logger.exception("Replay error for room %s", room_id)

    # ── Main run loop ────────────────────────────────────────────────────────

    async def run(self):
        await self._storage.open()
        await self._setup_client()
        await self._login()
        await self._setup_e2ee()

        self._auto_accept = AutoAccept(
            client=self._client,
            trusted_users=self._config.auto_accept_room_invites_from,
            auto_accept_dm=self._config.auto_accept_dm_invites,
        )
        self._event_handler = EventHandler(
            play_tracker=self._play_tracker,
            command_router=self._commands,
            bot_user_id=self._config.user_id,
        )
        self._event_handler._send_reply = self._send_message

        self._register_event_callbacks()
        self._register_verification_callbacks()

        # Initial sync to get current state
        logger.info("Performing initial sync…")
        await self._client.sync(timeout=30000, full_state=True)

        await self._replay_recent_timeline()

        self._play_tracker.start_background_tasks(
            drain_interval=self._config.queue_drain_interval_seconds,
            sweep_interval=self._config.abandoned_sweep_interval_secs,
        )

        logger.info("Scrobbler bot running. Syncing…")
        backoff = 5
        while True:
            try:
                await self._client.sync_forever(timeout=30000, full_state=False)
                backoff = 5
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("sync_forever error, retrying in %ds", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

        await self._play_tracker.stop()
        await self._scrobbler.close()
        await self._storage.close()
        await self._client.close()
