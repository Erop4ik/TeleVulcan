"""Фоновый опрос дневника для всех привязанных аккаунтов и рассылка изменений."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot

from .config import config
from .db import Database
from . import ui
from .pool import pool
from .vulcan.client import TZ, BadCredentials, VulcanError
from .vulcan.diff import (attendance_changes, homework_line, new_grades, new_items, plan_changes,
                          new_messages, semester_grade_changes, uwagi_new)

log = logging.getLogger(__name__)


def _chunks(lines: list[str], head: str, limit: int = 3500) -> list[str]:
    msgs, cur = [], head
    for ln in lines:
        if len(cur) + len(ln) + 1 > limit:
            msgs.append(cur)
            cur = head
        cur += "\n" + ln
    msgs.append(cur)
    return msgs


async def _send(bot: Bot, tg_id: int, head: str, lines: list[str]) -> None:
    if lines:
        for i in range(0, len(lines), 40):
            chunk = lines[i:i + 40]
            await ui.send_rich(bot, tg_id, ui.render_list_alert(head, chunk),
                               fallback="\n".join(_chunks(chunk, head)))


async def check_account(bot: Bot, db: Database, acc: dict) -> None:
    tg_id = acc["tg_id"]
    now = datetime.now(TZ)
    weeks = (now, now + timedelta(days=7))  # текущая и следующая неделя
    try:
        client = await pool.get(acc)
        try:
            # --- план: текущая + следующая неделя ---
            for label, day in zip(("plan_cur", "plan_next"), weeks):
                data = await client.plan(day)
                old = await db.get_snapshot(tg_id, label)
                await db.set_snapshot(tg_id, label, data)
                if old is not None:
                    await _send(bot, tg_id, "📚 Изменения в плане занятий:", plan_changes(old, data))

            # --- ДЗ и sprawdziany: две недели ---
            for label, day in zip(("hw_cur", "hw_next"), weeks):
                data = await client.homework(day)
                old = await db.get_snapshot(tg_id, label)
                await db.set_snapshot(tg_id, label, data)
                if old is not None:
                    lines = []
                    for x in new_items(old, data):
                        try:
                            det = await client.homework_details(x)
                        except VulcanError:
                            det = None
                        lines.append(homework_line(x, det))
                    await _send(bot, tg_id, "📝 Новое ДЗ / sprawdzian:", lines)

            # --- frekwencja: прошлая и текущая неделя (учителя вносят с опозданием) ---
            for label, day in (("att_prev", now - timedelta(days=7)), ("att_cur", now)):
                data = await client.attendance(day)
                old = await db.get_snapshot(tg_id, label)
                await db.set_snapshot(tg_id, label, data)
                if old is not None:
                    await _send(bot, tg_id, "🚫 Frekwencja: nieobecności / spóźnienia", attendance_changes(old, data))

            # --- оценки текущего семестра ---
            data = await client.grades()
            old = await db.get_snapshot(tg_id, "grades")
            await db.set_snapshot(tg_id, "grades", data)
            if old is not None:
                await _send(bot, tg_id, "🎓 Новые оценки:", new_grades(old, data))
                await _send(bot, tg_id, "🏁 Семестровые оценки:", semester_grade_changes(old, data))

            # --- uwagi ---
            data = await client.uwagi()
            old = await db.get_snapshot(tg_id, "uwagi")
            await db.set_snapshot(tg_id, "uwagi", data)
            if old is not None:
                await _send(bot, tg_id, "📣 Uwagi (замечания):", uwagi_new(old, data))

            # --- сообщения: новые письма во входящих, с текстом ---
            try:
                data = await client.messages()
                old = await db.get_snapshot(tg_id, "messages")
                await db.set_snapshot(tg_id, "messages", data)
                for m in reversed(new_messages(old, data)):  # старые первыми
                    try:
                        det = await client.message_details(m.get("apiGlobalKey"))
                    except VulcanError as e:
                        log.warning("tg=%s message %s: %s", tg_id, m.get("id"), e)
                        det = None
                    html, plain = ui.render_message_alert(m, det)
                    await ui.send_rich(bot, tg_id, html, fallback=plain)
            except VulcanError as e:
                log.warning("tg=%s wiadomości: %s", tg_id, e)

            await db.mark(tg_id, True, key=client.key)
        except BadCredentials as e:
            await pool.drop(tg_id)
            await db.mark(tg_id, False, f"пароль не подходит: {e}")
            await bot.send_message(tg_id, "Vulcan не принял логин/пароль. Привяжи аккаунт заново: /link")
        except VulcanError as e:
            await pool.drop(tg_id)
            await db.mark(tg_id, False, str(e))
            log.warning("tg=%s: %s", tg_id, e)
    except BadCredentials as e:
        await db.mark(tg_id, False, f"пароль не подходит: {e}")
        await bot.send_message(tg_id, "Vulcan не принял логин/пароль. Привяжи аккаунт заново: /link")


async def poll_forever(bot: Bot, db: Database) -> None:
    while True:
        try:
            for acc in await db.all_accounts():
                try:
                    await check_account(bot, db, acc)
                except Exception:
                    log.exception("poll failed for tg=%s", acc["tg_id"])
                await asyncio.sleep(3)  # не долбим Vulcan пачкой логинов
        except Exception:
            log.exception("poll loop error")
        await asyncio.sleep(config.poll_interval)
