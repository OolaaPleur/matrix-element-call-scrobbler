import asyncio
import logging
import time
from typing import Optional

import aiosqlite
from cryptography.fernet import Fernet

from scrobblers.base import UserCreds

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS linked_accounts (
  matrix_user_id        TEXT PRIMARY KEY,
  service               TEXT NOT NULL DEFAULT 'lastfm',
  service_username      TEXT NOT NULL,
  session_key_encrypted BLOB NOT NULL,
  enabled               INTEGER NOT NULL DEFAULT 1,
  linked_at             INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_links (
  matrix_user_id  TEXT PRIMARY KEY,
  service         TEXT NOT NULL DEFAULT 'lastfm',
  request_token   TEXT NOT NULL,
  created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS play_state (
  play_id         TEXT NOT NULL,
  matrix_user_id  TEXT NOT NULL,
  room_id         TEXT NOT NULL,
  started_at      INTEGER NOT NULL,
  artist          TEXT NOT NULL,
  track           TEXT NOT NULL,
  album           TEXT,
  duration_s      INTEGER NOT NULL,
  PRIMARY KEY (play_id, matrix_user_id)
);

CREATE TABLE IF NOT EXISTS scrobble_queue (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  matrix_user_id   TEXT NOT NULL,
  service          TEXT NOT NULL DEFAULT 'lastfm',
  artist           TEXT NOT NULL,
  track            TEXT NOT NULL,
  album            TEXT,
  duration_s       INTEGER,
  played_at        INTEGER NOT NULL,
  attempts         INTEGER NOT NULL DEFAULT 0,
  next_attempt_at  INTEGER NOT NULL,
  last_error       TEXT
);

CREATE TABLE IF NOT EXISTS recently_scrobbled (
  matrix_user_id  TEXT NOT NULL,
  artist          TEXT NOT NULL,
  track           TEXT NOT NULL,
  scrobbled_at    INTEGER NOT NULL,
  PRIMARY KEY (matrix_user_id, scrobbled_at)
);

CREATE TABLE IF NOT EXISTS room_blacklist (
  matrix_user_id  TEXT NOT NULL,
  room_id         TEXT NOT NULL,
  PRIMARY KEY (matrix_user_id, room_id)
);

CREATE INDEX IF NOT EXISTS idx_play_state_started_at ON play_state(started_at);
CREATE INDEX IF NOT EXISTS idx_scrobble_queue_next   ON scrobble_queue(next_attempt_at);
"""

BACKOFF = [60, 300, 1800, 7200, 21600]


class Storage:
    def __init__(self, db_path: str, encryption_key: str):
        self._db_path = db_path
        key = encryption_key.strip().encode() if isinstance(encryption_key, str) else encryption_key
        self._fernet = Fernet(key)
        self._db: Optional[aiosqlite.Connection] = None

    async def open(self):
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    def _encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def _decrypt(self, ciphertext: bytes) -> str:
        return self._fernet.decrypt(ciphertext).decode()

    # ── Linked accounts ──────────────────────────────────────────────────────

    async def get_creds(self, matrix_user_id: str, service: str = "lastfm") -> Optional[UserCreds]:
        async with self._db.execute(
            "SELECT service_username, session_key_encrypted, enabled FROM linked_accounts "
            "WHERE matrix_user_id=? AND service=?",
            (matrix_user_id, service),
        ) as cur:
            row = await cur.fetchone()
        if row is None or not row["enabled"]:
            return None
        return UserCreds(
            service=service,
            username=row["service_username"],
            secret=self._decrypt(row["session_key_encrypted"]),
        )

    async def save_linked_account(self, matrix_user_id: str, creds: UserCreds):
        encrypted = self._encrypt(creds.secret)
        await self._db.execute(
            "INSERT OR REPLACE INTO linked_accounts "
            "(matrix_user_id, service, service_username, session_key_encrypted, enabled, linked_at) "
            "VALUES (?,?,?,?,1,?)",
            (matrix_user_id, creds.service, creds.username, encrypted, int(time.time())),
        )
        await self._db.commit()

    async def delete_linked_account(self, matrix_user_id: str, service: str = "lastfm"):
        await self._db.execute(
            "DELETE FROM linked_accounts WHERE matrix_user_id=? AND service=?",
            (matrix_user_id, service),
        )
        await self._db.commit()

    async def set_enabled(self, matrix_user_id: str, enabled: bool, service: str = "lastfm"):
        await self._db.execute(
            "UPDATE linked_accounts SET enabled=? WHERE matrix_user_id=? AND service=?",
            (1 if enabled else 0, matrix_user_id, service),
        )
        await self._db.commit()

    async def get_linked_info(self, matrix_user_id: str, service: str = "lastfm") -> Optional[dict]:
        async with self._db.execute(
            "SELECT service_username, enabled, linked_at FROM linked_accounts "
            "WHERE matrix_user_id=? AND service=?",
            (matrix_user_id, service),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {"username": row["service_username"], "enabled": bool(row["enabled"]), "linked_at": row["linked_at"]}

    # ── Pending links ────────────────────────────────────────────────────────

    async def save_pending_link(self, matrix_user_id: str, request_token: str, service: str = "lastfm"):
        await self._db.execute(
            "INSERT OR REPLACE INTO pending_links (matrix_user_id, service, request_token, created_at) VALUES (?,?,?,?)",
            (matrix_user_id, service, request_token, int(time.time())),
        )
        await self._db.commit()

    async def pop_pending_link(self, matrix_user_id: str, service: str = "lastfm") -> Optional[str]:
        async with self._db.execute(
            "SELECT request_token FROM pending_links WHERE matrix_user_id=? AND service=?",
            (matrix_user_id, service),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        token = row["request_token"]
        await self._db.execute(
            "DELETE FROM pending_links WHERE matrix_user_id=? AND service=?",
            (matrix_user_id, service),
        )
        await self._db.commit()
        return token

    # ── Play state ───────────────────────────────────────────────────────────

    async def insert_play_state(self, play_id: str, matrix_user_id: str, room_id: str,
                                 started_at: int, artist: str, track: str,
                                 album: Optional[str], duration_s: int):
        await self._db.execute(
            "INSERT OR IGNORE INTO play_state "
            "(play_id, matrix_user_id, room_id, started_at, artist, track, album, duration_s) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (play_id, matrix_user_id, room_id, started_at, artist, track, album, duration_s),
        )
        await self._db.commit()

    async def get_play_state_rows(self, play_id: str) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM play_state WHERE play_id=?", (play_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_play_state(self, play_id: str):
        await self._db.execute("DELETE FROM play_state WHERE play_id=?", (play_id,))
        await self._db.commit()

    async def sweep_abandoned_plays(self, grace_seconds: int) -> int:
        now = int(time.time())
        async with self._db.execute(
            "SELECT play_id, matrix_user_id, artist, track, duration_s, started_at FROM play_state "
            "WHERE started_at < (? - 2 * duration_s - ?)",
            (now, grace_seconds),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            logger.warning(
                "Sweeping abandoned play_id=%s user=%s track='%s - %s'",
                row["play_id"], row["matrix_user_id"], row["artist"], row["track"],
            )
        if rows:
            play_ids = list({r["play_id"] for r in rows})
            await self._db.executemany(
                "DELETE FROM play_state WHERE play_id=?", [(pid,) for pid in play_ids]
            )
            await self._db.commit()
        return len(rows)

    async def drop_old_play_states(self, max_age_seconds: int):
        cutoff = int(time.time()) - max_age_seconds
        await self._db.execute("DELETE FROM play_state WHERE started_at < ?", (cutoff,))
        await self._db.commit()

    # ── Scrobble queue ───────────────────────────────────────────────────────

    async def enqueue_scrobble(self, matrix_user_id: str, service: str, artist: str,
                                track: str, album: Optional[str], duration_s: Optional[int],
                                played_at: int):
        now = int(time.time())
        await self._db.execute(
            "INSERT INTO scrobble_queue "
            "(matrix_user_id, service, artist, track, album, duration_s, played_at, attempts, next_attempt_at) "
            "VALUES (?,?,?,?,?,?,?,0,?)",
            (matrix_user_id, service, artist, track, album, duration_s, played_at, now),
        )
        await self._db.commit()

    async def get_due_scrobbles(self) -> list[dict]:
        now = int(time.time())
        async with self._db.execute(
            "SELECT * FROM scrobble_queue WHERE next_attempt_at <= ? ORDER BY id",
            (now,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_scrobble_queue_row(self, row_id: int):
        await self._db.execute("DELETE FROM scrobble_queue WHERE id=?", (row_id,))
        await self._db.commit()

    async def bump_scrobble_retry(self, row_id: int, attempts: int, error: str):
        if attempts >= len(BACKOFF):
            logger.error("Scrobble id=%d exhausted retries, dropping. Last error: %s", row_id, error)
            await self.delete_scrobble_queue_row(row_id)
            return
        delay = BACKOFF[attempts]
        next_at = int(time.time()) + delay
        await self._db.execute(
            "UPDATE scrobble_queue SET attempts=?, next_attempt_at=?, last_error=? WHERE id=?",
            (attempts + 1, next_at, error[:500], row_id),
        )
        await self._db.commit()

    # ── Recently scrobbled ───────────────────────────────────────────────────

    async def record_scrobble(self, matrix_user_id: str, artist: str, track: str, scrobbled_at: int):
        await self._db.execute(
            "INSERT OR REPLACE INTO recently_scrobbled (matrix_user_id, artist, track, scrobbled_at) VALUES (?,?,?,?)",
            (matrix_user_id, artist, track, scrobbled_at),
        )
        await self._db.commit()

    async def get_recent_scrobble(self, matrix_user_id: str, within_seconds: int = 3600) -> Optional[dict]:
        cutoff = int(time.time()) - within_seconds
        async with self._db.execute(
            "SELECT artist, track, scrobbled_at FROM recently_scrobbled "
            "WHERE matrix_user_id=? AND scrobbled_at >= ? ORDER BY scrobbled_at DESC LIMIT 1",
            (matrix_user_id, cutoff),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # ── Room blacklist ───────────────────────────────────────────────────────

    async def add_room_blacklist(self, matrix_user_id: str, room_id: str):
        await self._db.execute(
            "INSERT OR IGNORE INTO room_blacklist (matrix_user_id, room_id) VALUES (?,?)",
            (matrix_user_id, room_id),
        )
        await self._db.commit()

    async def remove_room_blacklist(self, matrix_user_id: str, room_id: str):
        await self._db.execute(
            "DELETE FROM room_blacklist WHERE matrix_user_id=? AND room_id=?",
            (matrix_user_id, room_id),
        )
        await self._db.commit()

    async def is_room_blacklisted(self, matrix_user_id: str, room_id: str) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM room_blacklist WHERE matrix_user_id=? AND room_id=?",
            (matrix_user_id, room_id),
        ) as cur:
            return await cur.fetchone() is not None

    async def get_blacklisted_rooms(self, matrix_user_id: str) -> list[str]:
        async with self._db.execute(
            "SELECT room_id FROM room_blacklist WHERE matrix_user_id=?", (matrix_user_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [r["room_id"] for r in rows]
