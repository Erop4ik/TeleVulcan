"""Публичный адрес страницы привязки.

Если PUBLIC_URL задан — используем его. Иначе спрашиваем у cloudflared Quick Tunnel
его текущий hostname через metrics-эндпоинт /quicktunnel (адрес меняется при каждом
рестарте туннеля, поэтому кешируем ненадолго)."""
from __future__ import annotations

import logging
import os
import time

import aiohttp

from .config import config

log = logging.getLogger(__name__)

METRICS_URL = os.environ.get("TUNNEL_METRICS_URL", "http://cloudflared:2000").rstrip("/")
_cache: tuple[str, float] | None = None


async def get_public_url() -> str:
    global _cache
    if config.public_url and "localhost" not in config.public_url and "127.0.0.1" not in config.public_url:
        return config.public_url
    if _cache and time.time() - _cache[1] < 60:
        return _cache[0]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get(f"{METRICS_URL}/quicktunnel") as r:
                data = await r.json(content_type=None)
        host = data.get("hostname")
        if host:
            url = f"https://{host}"
            _cache = (url, time.time())
            return url
    except Exception as ex:  # туннель ещё поднимается или не используется
        log.info("quick tunnel url unavailable: %s", ex)
    return config.public_url or "http://localhost:8765"
