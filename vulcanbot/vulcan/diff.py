"""Поиск изменений в ответах API и форматирование уведомлений.

Схемы (сняты зондом tools/probe.py, 17.09.2026):

PlanZajec -> [ {data, godzinaOd, godzinaDo, prowadzacy, prowadzacyWspomagajacy1/2, przedmiot,
               podzial, sala, pseudonim, zmiany: [...], zmianyUwagi: [...], adnotacja: int,
               dodatkowe: bool, zrealizowane: bool, idJednostkaSkladowa} ]
SprawdzianyZadaniaDomowe -> [ {typ: int (1 sprawdzian, 2 kartkówka?, 3 praca klasowa?, 4 zadanie domowe), przedmiotNazwa, data, hasAttachment, id} ]
Frekwencja -> {oddzialy: [{numerLekcji, kategoriaFrekwencji, data, opisZajec, nauczyciel, ...}], ...}

zmiany[] -> {zmiana: int (7 = zastępstwo), typProwadzacego, dzien, nrLekcji, godzinaOd,
             godzinaDo, grupa, zajecia, sala, prowadzacy, informacjeNieobecnosc};
adnotacja=1 сопровождает урок с zmiany. Другие коды zmiana пока не видели.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
# typ в SprawdzianyZadaniaDomowe: 1–3 — проверочные (детали: SprawdzianSzczegoly?id=),
# 4 — zadanie domowe (детали: ZadanieDomoweSzczegoly?id=). Подтверждены 1 (sprawdzian) и 4.
HW_TYPES = {1: "Sprawdzian", 2: "Kartkówka", 3: "Praca klasowa", 4: "Zadanie domowe"}
HOMEWORK_TYPE = 4


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _day(s: str | None) -> str:
    d = _dt(s)
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}" if d else (s or "?")


def _time(s: str | None) -> str:
    d = _dt(s)
    return f"{d:%H:%M}" if d else "?"


def _canon(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _plain(x: Any) -> str:
    if isinstance(x, (dict, list)):
        return json.dumps(x, ensure_ascii=False, separators=(", ", ": "))
    return str(x)


# ---------------- план ----------------

# Коды `zmiana` в PlanZajec. Подтверждён только 7 (zastępstwo, замена учителя).
# Остальные — догадка по UI Vulcan; неизвестный код печатается как есть.
ZMIANA = {
    1: "odwołane (отменено)",
    2: "przeniesione (перенесено)",
    3: "zmiana sali",
    4: "zmiana godziny",
    5: "zmiana zajęć",
    6: "dodatkowe zajęcia",
    7: "zastępstwo",
}


def change_text(z: dict) -> str:
    """Человекочитаемое описание одной записи из zmiany."""
    code = z.get("zmiana")
    parts = [ZMIANA.get(code, f"zmiana={code}")]
    if z.get("prowadzacy"):
        parts.append(f"→ {z['prowadzacy']}")
    if z.get("sala"):
        parts.append(f"s. {z['sala']}")
    if z.get("zajecia"):
        parts.append(str(z["zajecia"]))
    if z.get("dzien") or z.get("godzinaOd"):
        parts.append(f"{_day(z.get('dzien'))} {_time(z.get('godzinaOd'))}".strip())
    if z.get("grupa"):
        parts.append(f"[{z['grupa']}]")
    if z.get("informacjeNieobecnosc"):
        parts.append(str(z["informacjeNieobecnosc"]))
    return " ".join(parts)

def lesson_line(x: dict) -> str:
    """'пн 14.09 07:10–07:55 Edukacja dla bezpieczeństwa (Bieryłło Tomasz, s. 12)'."""
    who = x.get("prowadzacy") or ""
    sala = x.get("sala") or ""
    extra = ", ".join(p for p in (who, f"s. {sala}" if sala else "") if p)
    line = f"{_day(x.get('data'))} {_time(x.get('godzinaOd'))}–{_time(x.get('godzinaDo'))} {x.get('przedmiot') or '?'}"
    if extra:
        line += f" ({extra})"
    flags = [change_text(z) for z in (x.get("zmiany") or [])]
    if x.get("zmianyUwagi"):
        flags.append("uwagi: " + _plain(x["zmianyUwagi"]))
    if x.get("adnotacja") and not x.get("zmiany"):
        flags.append(f"adnotacja={x['adnotacja']}")
    if x.get("dodatkowe"):
        flags.append("dodatkowe zajęcia")
    return line + (" — " + "; ".join(flags) if flags else "")


def _slot(x: dict) -> str:
    return f"{x.get('data')}|{x.get('godzinaOd')}|{x.get('przedmiot')}|{x.get('podzial')}"


def plan_changes(old: list | None, new: list) -> list[str]:
    """Что поменялось между двумя снимками недели: новые/убранные уроки, замены, отмены."""
    if old is None:
        return []
    old_map = {_slot(x): x for x in old or []}
    new_map = {_slot(x): x for x in new or []}
    out: list[str] = []
    for slot, item in new_map.items():
        prev = old_map.get(slot)
        if prev is not None and _canon(prev) == _canon(item):
            continue
        if prev is None:
            out.append("➕ Новый урок: " + lesson_line(item))
            continue
        # тот же слот, но что-то изменилось
        changed = [k for k in item if item.get(k) != prev.get(k) and k != "zrealizowane"]
        if not changed:
            continue  # поменялся только флаг «zrealizowane»
        tag = "✏️ Изменение"
        if item.get("zmiany") or item.get("zmianyUwagi") or item.get("adnotacja"):
            tag = "🔁 Замена/изменение"
        if item.get("prowadzacy") != prev.get("prowadzacy"):
            tag += f" (учитель: {prev.get('prowadzacy')} → {item.get('prowadzacy')})"
        if item.get("sala") != prev.get("sala"):
            tag += f" (кабинет: {prev.get('sala') or '—'} → {item.get('sala') or '—'})"
        out.append(f"{tag}: {lesson_line(item)}")
    for slot, item in old_map.items():
        if slot not in new_map:
            out.append("❌ Урок убран/отменён: " + lesson_line(item))
    return out


def plan_text(items: list) -> str:
    """План недели, сгруппированный по дням."""
    days: dict[str, list[dict]] = {}
    for x in sorted(items or [], key=lambda x: (x.get("data") or "", x.get("godzinaOd") or "")):
        days.setdefault(_day(x.get("data")), []).append(x)
    parts = []
    for day, lessons in days.items():
        parts.append(f"📅 {day}")
        for x in lessons:
            ln = lesson_line(x)
            parts.append("  " + ln[len(day) + 1:] if ln.startswith(day) else "  " + ln)
    return "\n".join(parts) if parts else "План пуст."


# ---------------- ДЗ / sprawdziany ----------------

def homework_line(x: dict, details: Any = None) -> str:
    kind = HW_TYPES.get(x.get("typ"), f"typ {x.get('typ')}")
    line = f"{kind}: {x.get('przedmiotNazwa') or '?'} на {_day(x.get('data'))}"
    if x.get("hasAttachment"):
        line += " 📎"
    if isinstance(details, dict):
        if details.get("opis"):
            line += f"\n   {str(details['opis'])[:400]}"
        if details.get("nauczycielImieNazwisko"):
            line += f"\n   {details['nauczycielImieNazwisko']}"
    elif isinstance(details, str) and details.strip():
        line += f"\n   {details.strip()[:400]}"
    return line


def new_items(old: list | None, new: list, key: str = "id") -> list[dict]:
    """Элементы, которых не было в старом снимке (по id)."""
    if old is None:
        return []
    seen = {x.get(key) for x in old or []}
    return [x for x in new or [] if x.get(key) not in seen]


# ---------------- оценки ----------------
# Oceny -> {ocenyPrzedmioty: [{przedmiotNazwa, kolumnyOcenyCzastkowe: [{kategoriaKolumny,
#   nazwaKolumny, oceny: [{wpis, dataOceny "09.09.2026", waga, nauczyciel, kolorOceny,
#   idKolumny, idOcenaPoprawiona}]}], srednia, ocenaOkresowa, proponowanaOcenaOkresowa}]}

def flatten_grades(data: Any) -> list[dict]:
    out = []
    for subj in (data or {}).get("ocenyPrzedmioty", []) or []:
        for col in subj.get("kolumnyOcenyCzastkowe", []) or []:
            for g in col.get("oceny", []) or []:
                out.append({
                    "przedmiot": subj.get("przedmiotNazwa"),
                    "wpis": g.get("wpis"),
                    "data": g.get("dataOceny"),
                    "waga": g.get("waga"),
                    "kategoria": g.get("kategoriaKolumny") or col.get("kategoriaKolumny"),
                    "kolumna": g.get("nazwaKolumny") or col.get("nazwaKolumny"),
                    "nauczyciel": g.get("nauczyciel"),
                    "idKolumny": g.get("idKolumny"),
                    "poprawiona": g.get("idOcenaPoprawiona"),
                })
    return out


def grade_line(g: dict) -> str:
    w = g.get("waga")
    waga = f", waga {w:g}" if isinstance(w, (int, float)) else ""
    who = f" — {g['nauczyciel']}" if g.get("nauczyciel") else ""
    return (f"{g.get('przedmiot')}: {g.get('wpis')} ({g.get('kategoria') or '?'}: "
            f"{g.get('kolumna') or '?'}{waga}, {g.get('data')}){who}")


def _grade_key(g: dict) -> str:
    return _canon({k: g.get(k) for k in ("przedmiot", "idKolumny", "wpis", "data", "nauczyciel")})


def new_grades(old: Any, new: Any) -> list[str]:
    """Новые (и исправленные) оценки между снимками."""
    if old is None:
        return []
    seen = {_grade_key(g) for g in flatten_grades(old)}
    return [grade_line(g) for g in flatten_grades(new) if _grade_key(g) not in seen]


def grades_text(data: Any) -> str:
    """Все оценки семестра по предметам + средние."""
    parts = []
    for subj in (data or {}).get("ocenyPrzedmioty", []) or []:
        gs = flatten_grades({"ocenyPrzedmioty": [subj]})
        okresowa = str(subj.get("ocenaOkresowa") or "").strip()
        if not gs and not okresowa:
            continue
        head = f"📘 {subj.get('przedmiotNazwa')}"
        if subj.get("srednia"):
            head += f" (śr. {subj['srednia']:g})"
        if okresowa:
            head += f" — okresowa: {okresowa}"
        parts.append(head)
        for g in gs:
            parts.append(f"  {g.get('wpis')}  {g.get('kategoria') or ''}: {g.get('kolumna') or ''} ({g.get('data')})")
    return "\n".join(parts) if parts else "Оценок пока нет."


# ---------------- frekwencja ----------------
# Frekwencja?dataOd&dataDo -> {oddzialy: [{data, numerLekcji, godzinaOd, godzinaDo, opisZajec,
#   nauczyciel, kategoriaFrekwencji, idPoraLekcji, idLekcjaOddzial}], ...}
# Коды категорий — как в UONET+/Wulkanowy; подтверждены 1 и 2.
FREKWENCJA = {
    1: "obecność",
    2: "nieobecność nieusprawiedliwiona",
    3: "nieobecność usprawiedliwiona",
    4: "nieobecność z przyczyn szkolnych",
    5: "spóźnienie",
    6: "spóźnienie usprawiedliwione",
    7: "zwolnienie",
}
ABSENCE_ICON = {2: "❌", 3: "✅", 4: "🏫", 5: "⏰", 6: "⏰", 7: "🚪"}


def _att_rows(data: Any) -> list[dict]:
    rows = []
    for k in ("oddzialy", "dziennikiZajecInnych", "dziennikiSwietlicy"):
        rows.extend((data or {}).get(k) or [])
    return rows


def _att_key(r: dict) -> str:
    return f"{(r.get('data') or '')[:10]}|{r.get('numerLekcji')}|{r.get('idPoraLekcji')}"


def attendance_line(r: dict) -> str:
    cat = r.get("kategoriaFrekwencji")
    name = FREKWENCJA.get(cat, f"kategoria {cat}")
    icon = ABSENCE_ICON.get(cat, "•")
    return (f"{icon} {_day(r.get('data'))} lekcja {r.get('numerLekcji')} ({_time(r.get('godzinaOd'))}) "
            f"{r.get('opisZajec') or '?'}: {name}")


def attendance_changes(old: Any, new: Any) -> list[str]:
    """Новые или изменившиеся записи посещаемости, кроме обычного присутствия."""
    if old is None:
        return []
    prev = {_att_key(r): r.get("kategoriaFrekwencji") for r in _att_rows(old)}
    out = []
    for r in sorted(_att_rows(new), key=lambda r: (r.get("data") or "", r.get("numerLekcji") or 0)):
        cat = r.get("kategoriaFrekwencji")
        k = _att_key(r)
        if k in prev and prev[k] == cat:
            continue
        if cat == 1 and k not in prev:
            continue  # обычное присутствие не спамим
        line = attendance_line(r)
        if k in prev:
            line += f" (было: {FREKWENCJA.get(prev[k], prev[k])})"
        out.append(line)
    return out
