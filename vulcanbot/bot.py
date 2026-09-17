"""Telegram-бот: привязка через сайт, план/ДЗ/оценки с инлайн-навигацией (Rich Messages)."""
from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from . import ui
from .config import config
from .db import Database
from .pool import pool
from .publicurl import get_public_url
from .vulcan.client import TZ, BadCredentials, VulcanError

log = logging.getLogger(__name__)

WELCOME = (
    "Привет! Я слежу за дневником VULCAN (Uczeń): изменения плана, замены и отмены уроков, "
    "новые оценки и домашние задания.\n\n"
    "Чтобы привязать аккаунт, нажми /link — я дам ссылку на страницу, где ты введёшь логин "
    "и пароль от дневника. В Telegram они не попадают.\n\n"
    "Важно: сервис хранит логин и пароль в зашифрованном виде на сервере, потому что Vulcan "
    "не выдаёт долгоживущих токенов и сессия живёт ~20 минут. Отвязать и удалить данные: /unlink."
)


def make_dispatcher(db: Database) -> Dispatcher:
    dp = Dispatcher()

    async def account(m: Message | CallbackQuery) -> dict | None:
        acc = await db.get_account(m.from_user.id)
        if not acc:
            target = m if isinstance(m, Message) else m.message
            await target.answer("Сначала привяжи аккаунт: /link")
        return acc

    async def run(m: Message | CallbackQuery, acc: dict, coro_factory):
        """Выполнить запрос к Vulcan через пул сессий с единым разбором ошибок."""
        chat = m if isinstance(m, Message) else m.message
        try:
            client = await pool.get(acc)
            result = await coro_factory(client)
            await db.mark(acc["tg_id"], True, key=client.key)
            return result
        except BadCredentials as ex:
            await pool.drop(acc["tg_id"])
            await db.mark(acc["tg_id"], False, str(ex))
            await chat.answer("Vulcan не принял логин/пароль. Привяжи аккаунт заново: /link")
        except VulcanError as ex:
            await pool.drop(acc["tg_id"])
            await db.mark(acc["tg_id"], False, str(ex))
            await chat.answer(f"Дневник не ответил: {ex}")
        return None

    # ---------- базовые команды ----------

    @dp.message(CommandStart())
    async def start(m: Message):
        await m.answer(WELCOME, reply_markup=ui.MAIN_KB)

    @dp.message(Command("link"))
    async def link(m: Message):
        token = await db.create_link_token(m.from_user.id)
        base = await get_public_url()
        await m.answer(
            f"Открой ссылку и введи логин/пароль от дневника (действует 15 минут):\n"
            f"{base}/link/{token}", reply_markup=ui.MAIN_KB)

    @dp.message(Command("unlink"))
    async def unlink(m: Message):
        await pool.drop(m.from_user.id)
        await db.delete_account(m.from_user.id)
        await m.answer("Аккаунт отвязан, логин, пароль и снимки данных удалены.")

    @dp.message(Command("status"))
    @dp.message(F.text == ui.BTN_STATUS)
    async def status(m: Message):
        acc = await db.get_account(m.from_user.id)
        if not acc:
            await m.answer("Аккаунт не привязан. Нажми /link.")
            return
        ok = datetime.fromtimestamp(acc["last_ok"], TZ).strftime("%d.%m %H:%M") if acc["last_ok"] else "никогда"
        err = f"\nПоследняя ошибка: {acc['last_error']}" if acc["last_error"] else ""
        await m.answer(f"Логин: {acc['login']}\nПоследний успешный опрос: {ok}{err}\n"
                       f"Опрос каждые {config.poll_interval // 60} мин.", reply_markup=ui.MAIN_KB)

    # ---------- план ----------

    async def show(bot: Bot, m, html: str, kb):
        """Новое сообщение для команды, редактирование на месте для кнопки."""
        if isinstance(m, CallbackQuery) and m.message:
            if await ui.edit_rich(bot, m.message.chat.id, m.message.message_id, html, kb):
                return
            await ui.send_rich(bot, m.message.chat.id, html, kb)
            await _delete_quiet(m.message)
        else:
            await ui.send_rich(bot, m.chat.id, html, kb)

    async def show_plan(bot: Bot, chat_id: int, acc: dict, m, offset: int, day: int | None):
        items = await run(m, acc, lambda c: c.plan(ui.week_monday(offset)))
        if items is None:
            return
        html, kb = ui.render_plan(items, offset, day)
        await show(bot, m, html, kb)

    @dp.message(Command("plan"))
    @dp.message(F.text == ui.BTN_PLAN)
    async def plan(m: Message):
        acc = await account(m)
        if not acc:
            return
        offset = 1 if (m.text and "next" in m.text) else ui.default_offset()
        await show_plan(m.bot, m.chat.id, acc, m, offset, None if offset else ui.default_day())

    @dp.callback_query(F.data.startswith("p:"))
    async def plan_cb(cq: CallbackQuery):
        acc = await account(cq)
        if not acc:
            await cq.answer()
            return
        _, off, day = cq.data.split(":")
        await cq.answer()
        await show_plan(cq.bot, cq.message.chat.id, acc, cq, int(off), None if day == "a" else int(day))

    # ---------- ДЗ ----------

    async def show_hw(bot: Bot, chat_id: int, acc: dict, m, offset: int):
        async def fetch(c):
            items = await c.homework(ui.week_monday(offset))
            details = {}
            for x in items:
                try:
                    details[x["id"]] = await c.homework_details(x)
                except VulcanError:
                    details[x["id"]] = None
            return items, details
        res = await run(m, acc, fetch)
        if res is None:
            return
        items, details = res
        html, kb = ui.render_homework(items, offset, details)
        await show(bot, m, html, kb)

    @dp.message(Command("hw"))
    @dp.message(F.text == ui.BTN_HW)
    async def hw(m: Message):
        acc = await account(m)
        if not acc:
            return
        await show_hw(m.bot, m.chat.id, acc, m, 1 if (m.text and "next" in m.text) else ui.default_offset())

    @dp.callback_query(F.data.startswith("h:"))
    async def hw_cb(cq: CallbackQuery):
        acc = await account(cq)
        if not acc:
            await cq.answer()
            return
        await cq.answer()
        await show_hw(cq.bot, cq.message.chat.id, acc, cq, int(cq.data.split(":")[1]))

    # ---------- оценки ----------

    async def show_grades(bot: Bot, chat_id: int, acc: dict, m, period_id: int | None, subject: int | None):
        async def fetch(c):
            periods = await c.periods()
            pid = period_id or await c.current_period_id()
            return periods, pid, await c.grades(pid)
        res = await run(m, acc, fetch)
        if res is None:
            return
        periods, pid, data = res
        html, kb = ui.render_grades(data, periods, pid, subject)
        await show(bot, m, html, kb)

    @dp.message(Command("grades"))
    @dp.message(F.text == ui.BTN_GRADES)
    async def grades(m: Message):
        acc = await account(m)
        if not acc:
            return
        await show_grades(m.bot, m.chat.id, acc, m, None, None)

    @dp.callback_query(F.data.startswith("g:"))
    async def grades_cb(cq: CallbackQuery):
        acc = await account(cq)
        if not acc:
            await cq.answer()
            return
        _, pid, subj = cq.data.split(":")
        await cq.answer()
        await show_grades(cq.bot, cq.message.chat.id, acc, cq, int(pid), None if subj == "a" else int(subj))

    @dp.message(F.text)
    async def fallback(m: Message):
        await m.answer("Пользуйся кнопками внизу или командами: /plan, /hw, /grades, /status, /unlink",
                       reply_markup=ui.MAIN_KB)

    return dp


async def _delete_quiet(msg: Message) -> None:
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass


async def notify_linked(bot: Bot, tg_id: int, login: str) -> None:
    await bot.send_message(
        tg_id, f"Аккаунт {login} привязан. Первый опрос сделает базовый снимок, "
               f"дальше буду присылать только изменения.", reply_markup=ui.MAIN_KB)
