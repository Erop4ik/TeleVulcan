"""Рендер Rich Messages (Bot API 10.x, HTML-разметка с таблицами) и инлайн-клавиатур."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from html import escape as e, unescape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage,
                           KeyboardButton, ReplyKeyboardMarkup)

from .vulcan.client import TZ
from .vulcan.diff import HW_TYPES, ZMIANA, _dt, flatten_grades

# Необязательное локальное дополнение (не в репозитории): кабинеты из внешнего плана школы,
# если Vulcan не заполняет `sala`. Модуль должен давать `async refresh()` и `room_for(lesson)`.
try:
    from . import rooms_local as rooms  # type: ignore
except ImportError:
    rooms = None

log = logging.getLogger(__name__)

DAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
BTN_PLAN, BTN_HW, BTN_GRADES, BTN_STATUS = "📅 План", "📝 ДЗ", "🎓 Оценки", "ℹ️ Статус"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_PLAN), KeyboardButton(text=BTN_HW)],
              [KeyboardButton(text=BTN_GRADES), KeyboardButton(text=BTN_STATUS)]],
    resize_keyboard=True, is_persistent=True)


def week_monday(offset: int) -> datetime:
    now = datetime.now(TZ)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday + timedelta(weeks=offset)


def week_label(offset: int) -> str:
    mon = week_monday(offset)
    return f"{mon:%d.%m} – {mon + timedelta(days=6):%d.%m}"


def default_day() -> int:
    """Сегодня, а в выходные — понедельник следующей недели (см. default_offset)."""
    wd = datetime.now(TZ).weekday()
    return wd if wd < 5 else 0


def default_offset() -> int:
    return 0 if datetime.now(TZ).weekday() < 5 else 1


# ---------------- отправка ----------------

async def send_rich(bot: Bot, chat_id: int, html: str, kb: InlineKeyboardMarkup | None = None,
                    fallback: str | None = None):
    """Rich Message; если клиент/сервер не принял — обычное HTML-сообщение."""
    try:
        return await bot.send_rich_message(chat_id, InputRichMessage(html=html), reply_markup=kb)
    except TelegramBadRequest as ex:
        log.warning("rich message rejected (%s), fallback to plain", ex)
        text = fallback or _strip(html)
        for i in range(0, len(text), 3800):
            await bot.send_message(chat_id, text[i:i + 3800], reply_markup=kb if i == 0 else None,
                                   parse_mode="HTML")


async def edit_rich(bot: Bot, chat_id: int, message_id: int, html: str,
                    kb: InlineKeyboardMarkup | None = None) -> bool:
    """Редактирование rich-сообщения на месте (editMessageText + rich_message).
    False — если не вышло (старое сообщение, клиент без поддержки): тогда шлём новое."""
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                    rich_message=InputRichMessage(html=html), reply_markup=kb)
        return True
    except TelegramBadRequest as ex:
        if "not modified" in str(ex):
            return True
        log.info("edit_rich failed: %s", ex)
        return False


def btn(text: str, data: str, style: str | None = None) -> str:
    st = f' style="{style}"' if style else ""
    return f'<tg-button type="callback_data" data="{e(data)}"{st}>{e(text)}</tg-button>'


def btn_row(*buttons: str) -> str:
    return "<tg-button-row>" + "".join(b for b in buttons if b) + "</tg-button-row>"


def _strip(html: str) -> str:
    import re
    t = re.sub(r"</(tr|li|h\d|p|div)>", "\n", html)
    t = re.sub(r"</t[dh]>", "  ", t)
    t = re.sub(r"<hr\s*/?>", "\n———\n", t)
    t = re.sub(r"<(?!/?(b|i|s|code)>)[^>]+>", "", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


# ---------------- план ----------------

def _teacher_short(name: str | None) -> str:
    """Полное имя учителя как отдаёт Vulcan («Buchting Soza Roberto»); сокращения не делаем."""
    return name or ""


def _lesson_row(x: dict) -> str:
    subj = e(x.get("przedmiot") or "?")  # podzial (группа 1/2) намеренно не показываем
    teacher = _teacher_short(x.get("prowadzacy"))
    notes = []
    for z in x.get("zmiany") or []:
        kind = ZMIANA.get(z.get("zmiana"), f"zmiana {z.get('zmiana')}")
        if z.get("prowadzacy"):
            notes.append(f"⚠️ {e(kind)} → {e(_teacher_short(z['prowadzacy']))}")
            teacher = f"<s>{e(teacher)}</s>"
        else:
            notes.append(f"⚠️ {e(kind)}")
        if z.get("sala"):
            notes.append(f"s. {e(str(z['sala']))}")
    if x.get("zmianyUwagi"):
        notes.append("📝 " + e("; ".join(map(str, x["zmianyUwagi"]))))
    if x.get("dodatkowe"):
        notes.append("➕ dodatkowe")
    if notes:
        subj = f"<b>{subj}</b><br>" + " ".join(notes)
    elif not teacher.startswith("<s>"):
        teacher = e(teacher)
    sala = e(str(x.get("sala") or (rooms.room_for(x) if rooms else "") or ""))
    t = f"{_t(x.get('godzinaOd'))}<br>{_t(x.get('godzinaDo'))}"
    return f"<tr><td>{t}</td><td>{subj}</td><td>{teacher}{('<br>s. ' + sala) if sala else ''}</td></tr>"


def _t(s: str | None) -> str:
    d = _dt(s)
    return f"{d:%H:%M}" if d else "?"


def _by_day(items: list) -> dict[int, list[dict]]:
    days: dict[int, list[dict]] = {}
    for x in sorted(items or [], key=lambda x: (x.get("data") or "", x.get("godzinaOd") or "")):
        d = _dt(x.get("data"))
        if d:
            days.setdefault(d.weekday(), []).append(x)
    return days


def _day_table(lessons: list[dict]) -> str:
    rows = "".join(_lesson_row(x) for x in lessons)
    return f"<table bordered striped><tr><th>⏰</th><th>Предмет</th><th>Учитель</th></tr>{rows}</table>"


def render_plan(items: list, offset: int, day: int | None) -> tuple[str, InlineKeyboardMarkup]:
    days = _by_day(items)
    mon = week_monday(offset)
    if day is None:
        html = f"<h2>📅 Неделя {e(week_label(offset))}</h2>"
        for wd in range(7):
            if wd in days:
                html += f"<h3>{DAYS[wd]} {mon + timedelta(days=wd):%d.%m}</h3>{_day_table(days[wd])}"
        if not days:
            html += "<p>Уроков нет.</p>"
    else:
        html = f"<h2>📅 {DAYS[day]} {mon + timedelta(days=day):%d.%m}</h2>"
        html += _day_table(days[day]) if day in days else "<p>Уроков нет.</p>"
        changed = [x for x in days.get(day, []) if x.get("zmiany") or x.get("zmianyUwagi")]
        if changed:
            html += f"<p><i>Изменений в этот день: {len(changed)}</i></p>"
    day_btns = []
    for wd in range(5):
        has = any(x.get("zmiany") or x.get("zmianyUwagi") for x in days.get(wd, []))
        label = ("⚠️" if has else "") + DAYS[wd]
        style = "primary" if wd == day else ("danger" if has else None)
        day_btns.append(btn(label, f"p:{offset}:{wd}", style))
    if any(wd in days for wd in (5, 6)):
        day_btns.append(btn("Сб/Вс", f"p:{offset}:a"))
    # все кнопки внутри текста, внизу: сначала дни, под ними недели
    html += btn_row(*day_btns) + btn_row(
        btn("◀ неделя", f"p:{offset - 1}:a"),
        btn("вся неделя", f"p:{offset}:a", "primary" if day is None else None),
        btn("неделя ▶", f"p:{offset + 1}:a"))
    return html, None


# ---------------- ДЗ ----------------

def _hw_details_html(det) -> str:
    """Разворачиваемое описание: {opis, nauczycielImieNazwisko, linki[], ...}."""
    if isinstance(det, dict):
        parts = []
        if det.get("opis"):
            parts.append(e(str(det["opis"])))
        for k in ("tresc", "temat", "terminOddania"):
            if det.get(k):
                parts.append(f"<b>{e(k)}:</b> {e(str(det[k]))}")
        if det.get("nauczycielImieNazwisko") or det.get("nauczyciel"):
            parts.append(f"<i>{e(str(det.get('nauczycielImieNazwisko') or det.get('nauczyciel')))}</i>")
        for ln in det.get("linki") or []:
            url = ln.get("url") or ln.get("link") if isinstance(ln, dict) else str(ln)
            if url:
                parts.append(f'<a href="{e(url)}">{e(str((ln.get("nazwa") if isinstance(ln, dict) else None) or url))}</a>')
        if det.get("zalaczniki"):
            parts.append("📎 " + e(", ".join(str(z.get("nazwa", z)) if isinstance(z, dict) else str(z) for z in det["zalaczniki"])))
        body = "<br>".join(parts) or "<i>без описания</i>"
    elif isinstance(det, str) and det.strip():
        body = e(det.strip()[:1500])
    else:
        body = "<i>описание недоступно</i>"
    return f"<details><summary>описание</summary><p>{body}</p></details>"


def render_homework(items: list, offset: int, details: dict | None = None) -> tuple[str, None]:
    mon = week_monday(offset)
    html = f"<h2>📝 ДЗ и sprawdziany: {e(week_label(offset))}</h2>"
    by_day: dict[int, list[dict]] = {}
    for x in items or []:
        d = _dt(x.get("data"))
        if d:
            by_day.setdefault(d.weekday(), []).append(x)
    if not by_day:
        html += "<p>На эту неделю ничего нет 🎉</p>"
    for wd in sorted(by_day):
        html += f"<h3>{DAYS[wd]} {mon + timedelta(days=wd):%d.%m}</h3><ul>"
        for x in by_day[wd]:
            kind = HW_TYPES.get(x.get("typ"), f"typ {x.get('typ')}")
            icon = "📄" if x.get("typ") == 4 else "🧪"
            html += (f"<li>{icon} <b>{e(x.get('przedmiotNazwa') or '?')}</b> — {e(kind)}"
                     f"{' 📎' if x.get('hasAttachment') else ''}"
                     f"{_hw_details_html((details or {}).get(x.get('id'))) if details is not None else ''}</li>")
        html += "</ul>"
    html += btn_row(btn("◀ неделя", f"h:{offset - 1}"),
                    btn("сегодня", "h:0", "primary" if offset == 0 else None),
                    btn("неделя ▶", f"h:{offset + 1}"))
    return html, None


# ---------------- оценки ----------------

def render_grades(data: dict, periods: list[dict], period_id: int, subject: int | None) -> tuple[str, InlineKeyboardMarkup]:
    subjects = [s for s in (data or {}).get("ocenyPrzedmioty", []) or []
                if flatten_grades({"ocenyPrzedmioty": [s]}) or str(s.get("ocenaOkresowa") or "").strip()]
    per_num = next((p.get("numerOkresu") for p in periods if p.get("id") == period_id), "?")
    html = f"<h2>🎓 Оценки, semestr {per_num}</h2>"
    if not subjects:
        html += "<p>Оценок пока нет.</p>"
    shown = subjects if subject is None else [subjects[subject]] if subject < len(subjects) else []
    for s in shown:
        head = e(s.get("przedmiotNazwa") or "?")
        avg = s.get("srednia")
        if avg:
            head += f" <i>(śr. {avg:g})</i>"
        okres = str(s.get("ocenaOkresowa") or "").strip()
        prop = str(s.get("proponowanaOcenaOkresowa") or "").strip()
        html += f"<h3>{head}</h3>"
        if okres or prop:
            html += f"<p>{'Okresowa: <b>' + e(okres) + '</b> ' if okres else ''}{'Proponowana: <b>' + e(prop) + '</b>' if prop else ''}</p>"
        rows = ""
        for g in flatten_grades({"ocenyPrzedmioty": [s]}):
            w = g.get("waga")
            rows += (f"<tr><td><b>{e(str(g.get('wpis')))}</b></td><td>{e(g.get('kategoria') or '')}<br>"
                     f"<i>{e(g.get('kolumna') or '')}</i></td><td>{w:g}</td><td>{e(g.get('data') or '')}</td></tr>"
                     if isinstance(w, (int, float)) else
                     f"<tr><td><b>{e(str(g.get('wpis')))}</b></td><td>{e(g.get('kategoria') or '')}<br>"
                     f"<i>{e(g.get('kolumna') or '')}</i></td><td></td><td>{e(g.get('data') or '')}</td></tr>")
        if rows:
            html += f"<table bordered striped compact><tr><th>Ocena</th><th>Za co</th><th>Waga</th><th>Data</th></tr>{rows}</table>"
    subj_btns = []
    for i, s in enumerate(subjects):
        name = (s.get("przedmiotNazwa") or "?").replace("TP_", "")
        label = name[:16] + "…" if len(name) > 17 else name
        subj_btns.append(btn(label, f"g:{period_id}:{i}", "primary" if i == subject else None))
    rows = [btn_row(*subj_btns[i:i + 2]) for i in range(0, len(subj_btns), 2)]
    per = [btn(f"semestr {p.get('numerOkresu')}", f"g:{p.get('id')}:a",
               "primary" if p.get("id") == period_id and subject is None else None) for p in periods]
    if subject is not None:
        per.append(btn("все предметы", f"g:{period_id}:a"))
    html += "".join(rows) + btn_row(*per)
    return html, None


# ---------------- уведомления ----------------

def render_plan_alert(lines: list[str]) -> str:
    return "<h3>📚 Изменения в плане</h3><ul>" + "".join(f"<li>{e(l)}</li>" for l in lines) + "</ul>"


def render_list_alert(title: str, lines: list[str]) -> str:
    return f"<h3>{e(title)}</h3><ul>" + "".join(f"<li>{e(l)}</li>" for l in lines) + "</ul>"


def _msg_text(html: str | None) -> str:
    """HTML письма Vulcan -> плоский текст с переносами."""
    t = re.sub(r"(?i)<br\s*/?>", "\n", html or "")
    t = re.sub(r"(?i)</(p|div|li|h\d)>", "\n", t)
    t = unescape(re.sub(r"<[^>]+>", "", t)).replace("\xa0", " ")
    t = re.sub(r"\n{3,}", "\n\n", "\n".join(ln.rstrip() for ln in t.splitlines()))
    return t.strip()


def render_message_alert(meta: dict, det: dict | None) -> tuple[str, str]:
    """(rich html, plain fallback) для нового письма из Odebrane."""
    det = det or {}
    sender = det.get("nadawca") or meta.get("korespondenci") or "?"
    subject = det.get("temat") or meta.get("temat") or "(без темы)"
    d = _dt(meta.get("data") or det.get("data"))
    when = f"{d:%d.%m.%Y %H:%M}" if d else ""
    body = _msg_text(det.get("tresc"))
    if len(body) > 3000:
        body = body[:3000] + "…"
    files = [z.get("nazwaPliku") or z.get("nazwa") or "файл" for z in det.get("zalaczniki") or []]
    if not files and meta.get("hasZalaczniki"):
        files = ["есть вложения (смотри в дневнике)"]
    html = (f"<h3>✉️ Новое сообщение</h3>"
            f"<p><b>От:</b> {e(sender)}<br><b>Тема:</b> {e(subject)}<br><b>Дата:</b> {e(when)}</p>")
    html += ("<p>" + e(body).replace("\n", "<br>") + "</p>") if body else "<p><i>Текст не загрузился.</i></p>"
    if files:
        html += "<p>📎 " + e(", ".join(files)) + "</p>"
    plain = (f"✉️ <b>Новое сообщение</b>\nОт: {e(sender)}\nТема: {e(subject)}\nДата: {e(when)}\n\n{e(body)}"
             + (f"\n\n📎 {e(', '.join(files))}" if files else ""))
    return html, plain
