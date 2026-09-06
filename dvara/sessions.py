from __future__ import annotations

import json
import secrets

import aiosqlite

from .config import settings


class SessionStore:
    """Store storage_state (cookies + localStorage) per session_id.

    Gambling-site challenges set cookies (e.g. wtoken) that must survive
    across requests to the same domain. SQLite is enough for a single node;
    switch to Redis when running multi-node or needing TTL across workers.
    """

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None

    async def start(self) -> None:
        db = await aiosqlite.connect(settings.db_path)
        self._db = db
        await db.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            " session_id TEXT PRIMARY KEY,"
            " state TEXT NOT NULL,"
            " updated_at INTEGER NOT NULL)"
        )
        await db.commit()

    async def stop(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SessionStore is not started")
        return self._db

    def new_id(self) -> str:
        return secrets.token_hex(8)

    async def get(self, session_id: str) -> dict | None:
        async with self.db.execute(
            "SELECT state FROM sessions WHERE session_id = ?", (session_id,)
        ) as cur:
            row = await cur.fetchone()
        return json.loads(row[0]) if row else None

    async def set(self, session_id: str, state: dict) -> None:
        import time

        await self.db.execute(
            "INSERT INTO sessions (session_id, state, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
            (session_id, json.dumps(state), int(time.time())),
        )
        await self.db.commit()

    async def delete(self, session_id: str) -> bool:
        async with self.db.execute(
            "DELETE FROM sessions WHERE session_id = ?", (session_id,)
        ) as cur:
            await self.db.commit()
            return cur.rowcount > 0
