"""SQLite-хранилище: аккаунты (шифрованные логин/пароль), одноразовые токены привязки,
снимки данных для поиска изменений."""
from __future__ import annotations

import json
import secrets
import time
from typing import Any

import aiosqlite

from . import crypto
from .config import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    tg_id      INTEGER PRIMARY KEY,
    login_enc  TEXT NOT NULL,
    pass_enc   TEXT NOT NULL,
    key        TEXT,
    created_at INTEGER NOT NULL,
    last_ok    INTEGER,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS link_tokens (
    token      TEXT PRIMARY KEY,
    tg_id      INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    tg_id   INTEGER NOT NULL,
    feed    TEXT NOT NULL,
    data    TEXT NOT NULL,
    updated INTEGER NOT NULL,
    PRIMARY KEY (tg_id, feed)
);
"""


class Database:
    def __init__(self, path: str = config.db_path):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db
        return self._db

    # ---- link tokens ----

    async def create_link_token(self, tg_id: int) -> str:
        token = secrets.token_urlsafe(24)
        await self.db.execute("DELETE FROM link_tokens WHERE tg_id=? OR expires_at<?",
                              (tg_id, int(time.time())))
        await self.db.execute("INSERT INTO link_tokens VALUES (?,?,?)",
                              (token, tg_id, int(time.time()) + config.link_ttl))
        await self.db.commit()
        return token

    async def resolve_link_token(self, token: str) -> int | None:
        cur = await self.db.execute(
            "SELECT tg_id FROM link_tokens WHERE token=? AND expires_at>?", (token, int(time.time())))
        row = await cur.fetchone()
        return row["tg_id"] if row else None

    async def consume_link_token(self, token: str) -> None:
        await self.db.execute("DELETE FROM link_tokens WHERE token=?", (token,))
        await self.db.commit()

    # ---- accounts ----

    async def save_account(self, tg_id: int, login: str, password: str, key: str | None) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO accounts (tg_id, login_enc, pass_enc, key, created_at, last_ok) "
            "VALUES (?,?,?,?,?,?)",
            (tg_id, crypto.encrypt(login), crypto.encrypt(password), key, int(time.time()), int(time.time())))
        await self.db.execute("DELETE FROM snapshots WHERE tg_id=?", (tg_id,))
        await self.db.commit()

    async def delete_account(self, tg_id: int) -> None:
        await self.db.execute("DELETE FROM accounts WHERE tg_id=?", (tg_id,))
        await self.db.execute("DELETE FROM snapshots WHERE tg_id=?", (tg_id,))
        await self.db.commit()

    async def get_account(self, tg_id: int) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM accounts WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        return self._acc(row) if row else None

    async def all_accounts(self) -> list[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM accounts")
        return [self._acc(r) for r in await cur.fetchall()]

    @staticmethod
    def _acc(row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "tg_id": row["tg_id"],
            "login": crypto.decrypt(row["login_enc"]),
            "password": crypto.decrypt(row["pass_enc"]),
            "key": row["key"],
            "last_ok": row["last_ok"],
            "last_error": row["last_error"],
        }

    async def mark(self, tg_id: int, ok: bool, error: str | None = None, key: str | None = None) -> None:
        if ok:
            await self.db.execute(
                "UPDATE accounts SET last_ok=?, last_error=NULL, key=COALESCE(?, key) WHERE tg_id=?",
                (int(time.time()), key, tg_id))
        else:
            await self.db.execute("UPDATE accounts SET last_error=? WHERE tg_id=?", (error, tg_id))
        await self.db.commit()

    # ---- snapshots ----

    async def get_snapshot(self, tg_id: int, feed: str) -> Any | None:
        cur = await self.db.execute("SELECT data FROM snapshots WHERE tg_id=? AND feed=?", (tg_id, feed))
        row = await cur.fetchone()
        return json.loads(row["data"]) if row else None

    async def set_snapshot(self, tg_id: int, feed: str, data: Any) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?)",
            (tg_id, feed, json.dumps(data, ensure_ascii=False), int(time.time())))
        await self.db.commit()
