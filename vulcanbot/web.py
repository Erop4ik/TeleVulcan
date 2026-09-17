"""Страница привязки: /link/<token> — форма логин/пароль, проверка входом в Vulcan,
сохранение в зашифрованном виде и уведомление пользователя в Telegram."""
from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .bot import notify_linked
from .config import config
from .db import Database
from .vulcan.client import BadCredentials, VulcanClient, VulcanError

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def make_app(db: Database, bot: Bot) -> FastAPI:
    app = FastAPI(title="VulcanBot link")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(request, "index.html", {})

    @app.get("/link/{token}", response_class=HTMLResponse)
    async def link_form(request: Request, token: str):
        tg_id = await db.resolve_link_token(token)
        if tg_id is None:
            return templates.TemplateResponse(request, "link.html", {"expired": True}, status_code=410)
        return templates.TemplateResponse(request, "link.html", {"token": token, "symbol": config.symbol})

    @app.post("/link/{token}", response_class=HTMLResponse)
    async def link_submit(request: Request, token: str,
                          login: str = Form(...), password: str = Form(...), consent: str = Form("")):
        tg_id = await db.resolve_link_token(token)
        if tg_id is None:
            return templates.TemplateResponse(request, "link.html", {"expired": True}, status_code=410)
        ctx = {"token": token, "symbol": config.symbol, "login": login}
        if not consent:
            ctx["error"] = "Нужно согласиться с условиями хранения данных."
            return templates.TemplateResponse(request, "link.html", ctx)
        login = login.strip()
        try:
            async with VulcanClient(config.symbol, login, password) as c:
                await c.login_flow()
                key = c.key
        except BadCredentials as e:
            ctx["error"] = f"Vulcan не принял данные: {e}"
            return templates.TemplateResponse(request, "link.html", ctx)
        except VulcanError as e:
            log.exception("link login failed")
            ctx["error"] = f"Ошибка входа: {e}"
            return templates.TemplateResponse(request, "link.html", ctx)

        await db.save_account(tg_id, login, password, key)
        await db.consume_link_token(token)
        try:
            await notify_linked(bot, tg_id, login)
        except Exception:
            log.exception("notify failed")
        return templates.TemplateResponse(request, "done.html", {"login": login})

    return app
