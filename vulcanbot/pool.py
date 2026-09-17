"""Пул живых сессий Vulcan: один VulcanClient на пользователя, переиспользуется,
пока сессия не протухла (~20 минут простоя). Иначе каждое нажатие кнопки = новый логин с PoW."""
from __future__ import annotations

import asyncio
import logging
import time

from .config import config
from .vulcan.client import VulcanClient

log = logging.getLogger(__name__)

IDLE_TTL = 15 * 60


class ClientPool:
    def __init__(self) -> None:
        self._clients: dict[int, tuple[VulcanClient, float]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, tg_id: int) -> asyncio.Lock:
        return self._locks.setdefault(tg_id, asyncio.Lock())

    async def get(self, acc: dict) -> VulcanClient:
        tg_id = acc["tg_id"]
        async with self._lock(tg_id):
            entry = self._clients.get(tg_id)
            now = time.time()
            if entry and now - entry[1] < IDLE_TTL and entry[0].login == acc["login"]:
                self._clients[tg_id] = (entry[0], now)
                return entry[0]
            if entry:
                await entry[0].__aexit__(None, None, None)
            client = VulcanClient(config.symbol, acc["login"], acc["password"], key=acc["key"])
            await client.__aenter__()
            self._clients[tg_id] = (client, now)
            return client

    async def drop(self, tg_id: int) -> None:
        entry = self._clients.pop(tg_id, None)
        if entry:
            await entry[0].__aexit__(None, None, None)

    async def close_all(self) -> None:
        for tg_id in list(self._clients):
            await self.drop(tg_id)


pool = ClientPool()
