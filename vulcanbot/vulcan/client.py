"""Клиент нового дневника VULCAN «Uczeń» (dziennik-uczen.vulcan.net.pl).

Логин: dziennik-logowanie.vulcan.net.pl/<symbol>/Account/Logon (форма Login/Haslo +
PoW-капча) -> WS-Federation авто-форма (wa/wresult/wctx) -> dziennik-uczen /Account/Login ->
кука Dziennik.Uczen.Sso. Сессия живёт ~20 минут простоя, поэтому при 401/409/редиректе
на Login клиент перелогинивается сам.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import aiohttp

from . import pow

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
TZ = ZoneInfo("Europe/Warsaw")

_CAPTCHA_RE = re.compile(
    r'data-challenge="([^"]+)"\s+data-difficulty="(\d+)"\s+data-rounds="(\d+)"')
_KEY_RE = re.compile(r"/App/([A-Za-z0-9=_-]+)")
_ERR_RE = re.compile(
    r'class="[^"]*(?:message-error|messageInfo|messageSection|validation-summary-errors|field-validation-error)[^"]*"[^>]*>(.*?)</(?:div|span|ul)>',
    re.S)


class VulcanError(Exception):
    pass


class BadCredentials(VulcanError):
    pass


class NotLoggedIn(VulcanError):
    pass


class _FormParser(HTMLParser):
    """Первая <form> на странице: action и все input'ы с name.

    Регэкспом нельзя: в wresult символ '>' не экранирован, и <input[^>]*> обрывался."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.action: str | None = None
        self.inputs: dict[str, str] = {}
        self._forms = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form":
            self._forms += 1
            if self._forms == 1:
                self.action = a.get("action")
        elif tag == "input" and a.get("name"):
            self.inputs[a["name"]] = a.get("value", "")


def _parse_form(html: str) -> _FormParser:
    p = _FormParser()
    p.feed(html)
    return p


def _hidden_inputs(html: str) -> dict[str, str]:
    return _parse_form(html).inputs


def _form_action(html: str) -> str | None:
    return _parse_form(html).action


def decode_key(key: str) -> str:
    """key из URL — base64 (двойной!) от 'schoolId-studentId-x-y', например 12345-67890-1-17."""
    cur = key
    for _ in range(3):
        try:
            nxt = base64.b64decode(cur + "=" * (-len(cur) % 4)).decode("ascii")
        except Exception:
            break
        cur = nxt
        if _KEY_PLAIN_RE.match(cur):
            break
    return cur


_KEY_PLAIN_RE = re.compile(r"^\d+-\d+-\d+-\d+$")


def _find_key(obj: Any) -> str | None:
    """Рекурсивно ищем строку, которая base64-декодируется в 'schoolId-studentId-x-y'."""
    if isinstance(obj, str):
        if 8 <= len(obj) <= 64 and _KEY_PLAIN_RE.match(decode_key(obj)):
            return obj
        return None
    if isinstance(obj, dict):
        for k in ("key", "Key", "klucz", "Klucz"):
            if isinstance(obj.get(k), str) and _find_key(obj[k]):
                return obj[k]
        for v in obj.values():
            found = _find_key(v)
            if found:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = _find_key(v)
            if found:
                return found
    return None


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=TZ)


def _iso_utc(x: datetime) -> str:
    x = x.astimezone(ZoneInfo("UTC"))
    return x.strftime("%Y-%m-%dT%H:%M:%S.") + f"{x.microsecond // 1000:03d}Z"


def week_range(day: datetime | None = None) -> tuple[str, str]:
    """Неделя пн..вс так, как её шлёт фронтенд: границы по Europe/Warsaw, выданные в UTC."""
    d = (day or datetime.now(TZ)).astimezone(TZ)
    monday = (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    sunday_end = monday + timedelta(days=7) - timedelta(milliseconds=1)
    return _iso_utc(monday), _iso_utc(sunday_end)


@dataclass
class VulcanClient:
    symbol: str
    login: str
    password: str
    key: str | None = None
    id_dziennik: int | None = None
    global_key_skrzynka: str | None = None
    _session: aiohttp.ClientSession | None = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def base(self) -> str:
        return f"https://dziennik-uczen.vulcan.net.pl/{self.symbol}"

    async def __aenter__(self) -> "VulcanClient":
        self._session = aiohttp.ClientSession(
            headers={"User-Agent": UA, "Accept-Language": "pl,en;q=0.8"},
            # quote_cookie=False: aiohttp >= 3.10 оборачивает base64-значения кук в кавычки,
            # и ASP.NET на dziennik-logowanie отвечает 404 на Fs/Ls (проверено на 3.14.3)
            cookie_jar=aiohttp.CookieJar(quote_cookie=False),
            timeout=aiohttp.ClientTimeout(total=40),
            trust_env=True,  # HTTPS_PROXY/HTTP_PROXY из окружения — чтобы прятать IP сервера
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()

    @property
    def s(self) -> aiohttp.ClientSession:
        assert self._session, "используй `async with VulcanClient(...) as c`"
        return self._session

    # ---------------- login ----------------

    async def login_flow(self) -> None:
        async with self._lock:
            await self._login()

    async def _login(self) -> None:
        self.s.cookie_jar.clear()
        dbg = os.environ.get("VULCAN_DEBUG_DIR")
        if dbg and Path(dbg, "login_trace.txt").exists():
            Path(dbg, "login_trace.txt").unlink()
        # ровно как браузер: без returnUrl, иначе wtrealm не совпадает с настроенным в Fs/Ls
        start = f"{self.base}/Account/Login"
        async with self.s.get(start) as r:
            html = await r.text()
            logon_url = str(r.url)
        self._trace("start", r, html)
        if "dziennik-logowanie" not in logon_url:
            raise VulcanError(f"неожиданный адрес логина: {logon_url}")

        fields = _hidden_inputs(html)
        fields["Login"] = self.login
        fields["Haslo"] = self.password
        m = _CAPTCHA_RE.search(html)
        if m:
            challenge, difficulty, rounds = m.group(1), int(m.group(2)), int(m.group(3))
            fields["captcha-response"] = await asyncio.to_thread(
                pow.solve, challenge, difficulty, rounds)
        else:
            fields.setdefault("captcha-response", "")

        action = _form_action(html) or ""
        post_url = urljoin(logon_url, action) if action else logon_url
        async with self.s.post(post_url, data=fields, headers={"Referer": logon_url}) as r:
            html = await r.text()
            url = str(r.url)

        self._trace("logon-post", r, html)

        # WS-Federation: цепочка авто-форм с wresult, пока не окажемся в приложении
        for i in range(6):
            inputs = _hidden_inputs(html)
            if "wresult" in inputs or "wa" in inputs:
                action = _form_action(html) or url
                target = urljoin(url, action)
                async with self.s.post(target, data=inputs, headers={"Referer": url}) as r:
                    html = await r.text()
                    url = str(r.url)
                self._trace(f"wsfed-post-{i}", r, html)
                continue
            break

        if 'name="Haslo"' in html:
            # первый message-error на странице — скрытая подсказка про eduVULCAN, берём остальные
            msgs = [" ".join(unescape(re.sub("<[^>]+>", " ", m.group(1))).split())
                    for m in _ERR_RE.finditer(html)]
            msgs = [t for t in msgs if t and "eduvulcan.pl" not in t.lower()]
            raise BadCredentials(msgs[-1] if msgs else "Vulcan не принял логин/пароль")

        if not self._has_sso():
            dbg = os.environ.get("VULCAN_DEBUG_DIR")
            if dbg:
                Path(dbg).mkdir(exist_ok=True)
                Path(dbg, "last_login_page.html").write_text(html, "utf-8")
            raise VulcanError(f"после логина нет куки Dziennik.Uczen.Sso (url={url}); "
                              f"страница сохранена в {dbg or 'VULCAN_DEBUG_DIR (не задан)'}")

        km = _KEY_RE.search(url) or _KEY_RE.search(html)
        if km:
            self.key = km.group(1)
        if not self.key:
            self.key = await self._discover_key()
        log.info("login ok: %s key=%s (%s)", self.login, self.key, decode_key(self.key or ""))

    def _trace(self, step: str, r: aiohttp.ClientResponse, html: str) -> None:
        """Отладочный след логина в VULCAN_DEBUG_DIR/login_trace.txt (без паролей/токенов)."""
        dbg = os.environ.get("VULCAN_DEBUG_DIR")
        if not dbg:
            return
        lines = [f"=== {step}"]
        for h in list(r.history) + [r]:
            lines.append(f"{h.status} {h.method} {h.url}")
            for k, v in h.headers.items():
                if k.lower() in ("set-cookie", "location"):
                    lines.append(f"    {k}: {v[:90]}{'...' if len(v) > 90 else ''}")
        lines.append("jar: " + ", ".join(sorted(f"{c.key}@{c['domain'] or '-'}" for c in self.s.cookie_jar)))
        lines.append(f"html: {len(html)} bytes, title={re.search(r'<title>(.*?)</title>', html, re.S).group(1).strip()[:60] if '<title>' in html else '-'}")
        Path(dbg).mkdir(exist_ok=True)
        with open(Path(dbg, "login_trace.txt"), "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _has_sso(self) -> bool:
        return any(c.key == "Dziennik.Uczen.Sso" for c in self.s.cookie_jar)

    async def _discover_key(self) -> str:
        """key ученика: сначала /App/<key> в URL, иначе api/Context (эндпоинт без key,
        который SPA дёргает при старте) — ищем в ответе base64 от 'a-b-c-d'."""
        async with self.s.get(f"{self.base}/App", allow_redirects=True) as r:
            html = await r.text()
            m = _KEY_RE.search(str(r.url)) or _KEY_RE.search(html)
            if m:
                return m.group(1)
        async with self.s.get(f"{self.base}/api/Context",
                              headers={"Accept": "application/json, text/plain, */*",
                                       "Referer": f"{self.base}/App"}) as r:
            body = await r.text()
            dbg = os.environ.get("VULCAN_DEBUG_DIR")
            if dbg:
                Path(dbg).mkdir(exist_ok=True)
                Path(dbg, "Context.json").write_text(body, "utf-8")
            if r.status == 200:
                try:
                    ctx = json.loads(body)
                    found = _find_key(ctx)
                except ValueError:
                    ctx, found = None, None
                if found:
                    for u in (ctx or {}).get("uczniowie", []):
                        if u.get("key") == found:
                            self.id_dziennik = u.get("idDziennik")
                    return found
        raise VulcanError(f"не удалось определить key ученика (api/Context -> HTTP {r.status})")

    # ---------------- api ----------------

    async def api(self, name: str, **params: Any) -> Any:
        """GET /<symbol>/api/<name>?key=...&params. Перелогин при протухшей сессии.
        Не-JSON ответ (HTML заглушки и т.п.) возвращается строкой."""
        for attempt in range(2):
            if not self._has_sso() or not self.key:
                await self.login_flow()
            q = {"key": self.key, **{k: v for k, v in params.items() if v is not None}}
            async with self.s.get(
                f"{self.base}/api/{name}", params=q,
                headers={"Accept": "application/json, text/plain, */*",
                         "Referer": f"{self.base}/App/{self.key}/"},
                allow_redirects=False,
            ) as r:
                if r.status in (301, 302, 401, 403, 409) and attempt == 0:
                    log.info("session expired (%s) -> re-login", r.status)
                    self.s.cookie_jar.clear()
                    continue
                body = await r.text()
                if r.status != 200:
                    raise VulcanError(f"{name}: HTTP {r.status} {body[:200]!r}")
                try:
                    return json.loads(body) if body.strip() else None
                except ValueError:
                    return body
        raise NotLoggedIn(name)

    async def _vparam(self) -> dict[str, str]:
        """antiForgeryToken/appGuid из window.VParam на странице /App (нужны для POST)."""
        async with self.s.get(f"{self.base}/App") as r:
            html = await r.text()
        out = {}
        for k in ("antiForgeryToken", "appGuid"):
            m = re.search(k + r"\s*:\s*'([^']*)'", html)
            if m:
                out[k] = m.group(1)
        if len(out) < 2:
            raise VulcanError("VParam не найден на /App")
        return out

    async def api_post(self, name: str, **body: Any) -> Any:
        """POST /<symbol>/api/<name> с JSON-телом и anti-forgery заголовками (как axios в SPA)."""
        if not self._has_sso() or not self.key:
            await self.login_flow()
        vp = await self._vparam()
        payload = {"key": self.key, **{k: v for k, v in body.items() if v is not None}}
        async with self.s.post(
            f"{self.base}/api/{name}", json=payload,
            headers={"Accept": "application/json, text/plain, */*",
                     "X-V-RequestVerificationToken": vp["antiForgeryToken"],
                     "X-V-AppGuid": vp["appGuid"],
                     "Referer": f"{self.base}/App/{self.key}/"},
            allow_redirects=False,
        ) as r:
            text = await r.text()
            if r.status != 200:
                raise VulcanError(f"POST {name}: HTTP {r.status} {text[:200]!r}")
            try:
                return json.loads(text) if text.strip() else None
            except ValueError:
                return text

    # ---- известные эндпоинты (схемы см. README, раздел «API») ----

    async def plan(self, day: datetime | None = None) -> list[dict]:
        """PlanZajec: список уроков недели. Поля: data, godzinaOd, godzinaDo, przedmiot,
        prowadzacy, sala, podzial, zmiany[], zmianyUwagi[], adnotacja, dodatkowe, zrealizowane."""
        od, do = week_range(day)
        return await self.api("PlanZajec", dataOd=od, dataDo=do, zakresDanych=2) or []

    async def homework(self, day: datetime | None = None) -> list[dict]:
        """SprawdzianyZadaniaDomowe: [{typ, przedmiotNazwa, data, hasAttachment, id}].
        typ 1..3 — проверочные, 4 — zadanie domowe (см. diff.HW_TYPES)."""
        od, do = week_range(day)
        return await self.api("SprawdzianyZadaniaDomowe", dataOd=od, dataDo=do) or []

    async def homework_details(self, item: dict) -> Any:
        """Детали: для zadanie domowe (typ 4) — ZadanieDomoweSzczegoly?id=, для проверочных —
        SprawdzianSzczegoly?id= -> {typ, data, przedmiotNazwa, nauczycielImieNazwisko, opis, linki[], id}."""
        name = "ZadanieDomoweSzczegoly" if item.get("typ") == 4 else "SprawdzianSzczegoly"
        return await self.api(name, id=item["id"])

    async def attendance(self, day: datetime | None = None) -> Any:
        od, do = week_range(day)
        return await self.api("Frekwencja", dataOd=od, dataDo=do)

    async def periods(self) -> list[dict]:
        """OkresyKlasyfikacyjne?idDziennik -> [{numerOkresu, dataOd, dataDo, id}]."""
        if self.id_dziennik is None:
            await self._load_context()
        return await self.api("OkresyKlasyfikacyjne", idDziennik=self.id_dziennik) or []

    async def current_period_id(self) -> int:
        now = datetime.now(TZ)
        per = await self.periods()
        for o in per:
            od, do = _parse_dt(o.get("dataOd")), _parse_dt(o.get("dataDo"))
            if od and do and od <= now <= do + timedelta(days=1):
                return o["id"]
        if not per:
            raise VulcanError("OkresyKlasyfikacyjne: пустой список периодов")
        return per[-1]["id"]

    async def grades(self, period_id: int | None = None) -> dict:
        """Oceny?idDziennik&idOkresKlasyfikacyjny ->
        {ocenyPrzedmioty: [{przedmiotNazwa, kolumnyOcenyCzastkowe: [{kategoriaKolumny, nazwaKolumny,
         oceny: [{wpis, dataOceny, waga, nauczyciel, kolorOceny, idKolumny, idOcenaPoprawiona}]}],
         srednia, ocenaOkresowa, proponowanaOcenaOkresowa, ...}], ustawienia: {...}}"""
        if self.id_dziennik is None:
            await self._load_context()
        pid = period_id or await self.current_period_id()
        return await self.api("Oceny", idDziennik=self.id_dziennik, idOkresKlasyfikacyjny=pid) or {}

    async def _load_context(self) -> None:
        """api/Context: key + idDziennik ученика (без key). При 401/409 — перелогин и повтор."""
        ctx: dict = {}
        for attempt in range(2):
            if not self._has_sso():
                await self.login_flow()
            async with self.s.get(f"{self.base}/api/Context",
                                  headers={"Accept": "application/json, text/plain, */*",
                                           "Referer": f"{self.base}/App"},
                                  allow_redirects=False) as r:
                if r.status in (301, 302, 401, 403, 409) and attempt == 0:
                    log.info("Context: session expired (%s) -> re-login", r.status)
                    self.s.cookie_jar.clear()
                    continue
                if r.status != 200:
                    raise VulcanError(f"Context: HTTP {r.status}")
                ctx = await r.json(content_type=None)
            break
        students = ctx.get("uczniowie") or []
        me = next((u for u in students if u.get("key") == self.key), students[0] if students else None)
        if not me:
            raise VulcanError("Context: нет учеников")
        self.key = self.key or me.get("key")
        self.id_dziennik = me.get("idDziennik")
        self.global_key_skrzynka = me.get("globalKeySkrzynka")

    # ---------------- wiadomości (dziennik-wiadomosci) ----------------

    @property
    def wbase(self) -> str:
        return f"https://dziennik-wiadomosci.vulcan.net.pl/{self.symbol}"

    def _has_wsso(self) -> bool:
        return any(c.key == "Dziennik.Wiadomosci.Sso" for c in self.s.cookie_jar)

    async def _wiadomosci_login(self) -> None:
        """SSO в модуль сообщений: STS уже помнит нас (Vulcan.Efeb.Logowanie.Web),
        поэтому Fs/Ls сразу отдаёт WS-Fed форму для realm dziennik-wiadomosci."""
        if not self._has_sso():
            await self.login_flow()
        async with self.s.get(f"{self.wbase}/Account/Login?returnUrl=/{self.symbol}/App") as r:
            html = await r.text()
            url = str(r.url)
        for _ in range(5):
            inputs = _hidden_inputs(html)
            if "wresult" not in inputs:
                break
            target = urljoin(url, _form_action(html) or url)
            async with self.s.post(target, data=inputs, headers={"Referer": url}) as r:
                html = await r.text()
                url = str(r.url)
        if not self._has_wsso():
            raise VulcanError(f"wiadomości: нет куки Dziennik.Wiadomosci.Sso (url={url})")

    async def wapi(self, name: str, **params: Any) -> Any:
        """GET dziennik-wiadomosci/<symbol>/api/<name>. Перелогин при протухшей сессии."""
        for attempt in range(2):
            if not self._has_wsso():
                await self._wiadomosci_login()
            q = {k: v for k, v in params.items() if v is not None}
            async with self.s.get(
                f"{self.wbase}/api/{name}", params=q,
                headers={"Accept": "application/json, text/plain, */*", "Referer": f"{self.wbase}/App"},
                allow_redirects=False,
            ) as r:
                if r.status in (301, 302, 401, 403, 409) and attempt == 0:
                    log.info("wiadomości: session expired (%s) -> re-login", r.status)
                    self.s.cookie_jar.clear()
                    continue
                body = await r.text()
                if r.status != 200:
                    raise VulcanError(f"wiadomości {name}: HTTP {r.status} {body[:200]!r}")
                try:
                    return json.loads(body) if body.strip() else None
                except ValueError:
                    return body
        raise NotLoggedIn(name)

    async def mailboxes(self) -> list[dict]:
        """Skrzynki -> [{globalKey, nazwa, typUzytkownika}]."""
        return await self.wapi("Skrzynki") or []

    async def messages(self, page_size: int = 20) -> list[dict]:
        """Odebrane -> [{apiGlobalKey, id, korespondenci, temat, data, hasZalaczniki, przeczytana, ...}]."""
        return await self.wapi("Odebrane", idLastWiadomosc=0, pageSize=page_size) or []

    async def message_details(self, api_global_key: str) -> dict:
        """WiadomoscSzczegoly -> {nadawca, odbiorcy, temat, tresc (HTML), data, zalaczniki, ...}."""
        return await self.wapi("WiadomoscSzczegoly", apiGlobalKey=api_global_key) or {}

    async def unread_counts(self) -> list[dict]:
        """LiczbyNieodczytanych -> [{globalKey, liczbaWiadomosci}]."""
        return await self.wapi("LiczbyNieodczytanych") or []

    async def uwagi(self) -> Any:
        """Uwagi (замечания/похвалы). Схема пока не снята — у тестового аккаунта пусто."""
        return await self.api("Uwagi") or []


def pretty(obj: Any, limit: int = 3000) -> str:
    s = json.dumps(obj, ensure_ascii=False, indent=1)
    return s if len(s) <= limit else s[:limit] + "..."
