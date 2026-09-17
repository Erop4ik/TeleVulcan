"""Запуск: python -m vulcanbot  (бот + сайт привязки + опросчик в одном процессе)."""
import asyncio
import logging
import os

import uvicorn
from aiogram import Bot

from .bot import make_dispatcher
from .config import config
from .db import Database
from .poller import poll_forever
from .pool import pool
from .web import make_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main() -> None:
    if not config.bot_token or not config.secret_key:
        raise SystemExit("Заполни BOT_TOKEN и SECRET_KEY в .env (см. .env.example)")
    db = Database()
    await db.open()
    bot = Bot(config.bot_token)
    dp = make_dispatcher(db)
    server = uvicorn.Server(uvicorn.Config(make_app(db, bot), host=os.environ.get("WEB_HOST", "0.0.0.0"),
                                           port=config.web_port, log_level="info"))
    try:
        await asyncio.gather(
            dp.start_polling(bot),
            server.serve(),
            poll_forever(bot, db),
        )
    finally:
        await pool.close_all()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
