# TeleVulcan

> 🇷🇺 Русская версия: [README.ru.md](README.ru.md)

Telegram bot for the new **VULCAN "Uczeń"** e-diary (`dziennik-uczen.vulcan.net.pl`) used by Polish schools.
It watches the timetable (substitutions, cancellations, room/teacher changes), new homework and tests,
grades and attendance, and sends only what changed.

## Why the bot needs your password

Regular VULCAN accounts (login like `ABCDEFG-123456`) get **no API tokens** and there is no OAuth for
third-party apps. Every cookie is session-bound and expires after ~20 minutes of inactivity; there is no
way to refresh it without the login and password again. Therefore:

- the user enters the login and password **not in Telegram**, but on the bot's `/link/<token>` page;
- the bot verifies them with a real login, encrypts them (Fernet, key from `SECRET_KEY`) and stores them in SQLite;
- every poll logs in again when needed; `/unlink` deletes everything.

The link page says this explicitly and requires a consent checkbox.

## How the login works

1. `GET dziennik-uczen/<symbol>/Account/Login` → redirect to `dziennik-logowanie/<symbol>/Account/Logon`.
2. Form fields `Login`, `Haslo`, `__RequestVerificationToken`, `captcha-response`.
   The "captcha" is proof-of-work from `captcha.js`: 15 rounds of SHA-256, find a nonce whose first
   4 hash bytes are `< difficulty`. Solved in ~0.7 s (`vulcanbot/vulcan/pow.py`).
3. The response is a WS-Federation auto-post form (`wa`, `wresult`, `wctx`); the client posts it to
   `dziennik-uczen/.../Account/Login`, which sets the `Dziennik.Uczen.Sso` cookie.
   Note: `wresult` contains unescaped `>` inside the attribute, so forms are parsed with a real HTML parser.
4. The student `key` comes from `GET api/Context` (`uczniowie[0].key`), see "API".
5. API: `GET /<symbol>/api/<Name>?key=<key>&...`. On 401/409/redirect the client logs in again.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in BOT_TOKEN, PUBLIC_URL, SECRET_KEY, VULCAN_SYMBOL
python -m vulcanbot
```

`SECRET_KEY`:

```bash
python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
```

The link page listens on `WEB_PORT` (default 8080). Expose it **only over HTTPS** (nginx/Caddy/Cloudflare
Tunnel); `PUBLIC_URL` is what users will see.

## Interface

The bot uses **Rich Messages** (Bot API 10.x, aiogram ≥ 3.31): headings, bordered tables, lists,
expandable `<details>` blocks and `<tg-button>` buttons inside the message body. Navigation edits the
message in place (`editMessageText` with `rich_message`). If the client rejects a rich message,
the bot falls back to plain HTML text.

Persistent bottom keyboard: **📅 Plan · 📝 Homework · 🎓 Grades · ℹ️ Status**.

| Screen | Navigation |
|---|---|
| Timetable | day buttons Mon…Fri (⚠️ / red = substitutions that day), ◀ week / whole week / week ▶ |
| Homework & tests | ◀ week / today / week ▶; each item has an expandable description |
| Grades | subjects with grades, semester 1/2 switch, "all subjects" |

Commands: `/start`, `/link`, `/plan [next]`, `/hw [next]`, `/grades`, `/status`, `/unlink`.

VULCAN sessions live in a pool (`vulcanbot/pool.py`) for up to 15 idle minutes, so button presses do not
trigger a new PoW login every time.

## Notifications

Every `POLL_INTERVAL` seconds (default 300) the bot compares snapshots and sends:

- 📚 timetable changes: substitutions, cancellations, new/removed lessons, teacher or room changes (current + next week);
- 📝 new homework and tests with descriptions (two weeks);
- 🚫 attendance: new absences, lateness, exemptions and category changes, e.g. when an absence becomes excused (previous + current week);
- 🎓 new grades in the current semester.

## API (captured with the probe, 2026-09-17)

All requests: `GET https://dziennik-uczen.vulcan.net.pl/<symbol>/api/<Name>?key=<key>&...`,
dates in ISO UTC, week bounds in Europe/Warsaw (`2026-09-13T22:00:00.000Z`…`2026-09-20T21:59:59.999Z`).

| Endpoint | Params | Response | Status |
|---|---|---|---|
| `Context` | no key | `{uczniowie:[{key, uczen, oddzial, jednostka, idDziennik, config{...}}]}` | confirmed, source of `key` |
| `PlanZajec` | `dataOd, dataDo, zakresDanych=2` | `[{data, godzinaOd, godzinaDo, przedmiot, prowadzacy, sala, podzial, zmiany[], zmianyUwagi[], adnotacja, dodatkowe, zrealizowane}]` | confirmed |
| `SprawdzianyZadaniaDomowe` | `dataOd, dataDo` | `[{typ, przedmiotNazwa, data, hasAttachment, id}]`; typ 1 sprawdzian, 2 kartkówka, 3 praca klasowa (?), 4 zadanie domowe | confirmed |
| `SprawdzianSzczegoly` | `id` (typ 1–3) | `{typ, data, przedmiotNazwa, nauczycielImieNazwisko, opis, linki[], id}` | confirmed |
| `ZadanieDomoweSzczegoly` | `id` (typ 4) | same shape; returns 500 for a test id | confirmed |
| `Frekwencja` | `dataOd, dataDo` | `{oddzialy:[{numerLekcji, kategoriaFrekwencji, data, opisZajec, nauczyciel}], ...}` | confirmed |
| `Uwagi` | — | `[]` | confirmed |
| `OkresyKlasyfikacyjne` | `idDziennik` | `[{numerOkresu, dataOd, dataDo, id}]` | confirmed |
| `Oceny` | `idDziennik, idOkresKlasyfikacyjny` | `{ocenyPrzedmioty:[{przedmiotNazwa, kolumnyOcenyCzastkowe:[{kategoriaKolumny, nazwaKolumny, oceny:[{wpis, dataOceny, waga, nauczyciel, kolorOceny, idKolumny, idOcenaPoprawiona}]}], srednia, ocenaOkresowa, proponowanaOcenaOkresowa}], ustawienia}` | confirmed |

`idDziennik` comes from `Context.uczniowie[].idDziennik`. `key` is double base64 of `schoolId-studentId-x-y`
(`TVRJ…` → `MTIz…` → `12345-67890-1-17`).

A teacher substitution in `PlanZajec` looks like:

```json
"zmiany": [{"zmiana": 7, "typProwadzacego": 1, "prowadzacy": "Kowalska Anna", "sala": "", ...}], "adnotacja": 1
```

Attendance categories (`kategoriaFrekwencji`, as in UONET+/Wulkanowy): 1 present, 2 unexcused absence,
3 excused absence, 4 absence for school reasons, 5/6 lateness, 7 exemption. 1 and 2 are confirmed.

Controller names were extracted from the SPA bundle (`prodbundle_*.js`: the static `ApiName` field and
view names). The probe logs in and dumps responses to `probe_out/` (git-ignored):

```bash
python tools/probe.py
```

## Limitations and ethics

- Unofficial client. VULCAN may change the login form or the API at any time.
- Do not poll more often than every few minutes (`POLL_INTERVAL`) to avoid account lockouts.
- Do not run a public instance without HTTPS and without understanding that you store other people's passwords.
- The bot never requests `DaneUcznia` or `Konto` (birth date, e-mail, …) and stores nothing except
  the timetable/homework/grade/attendance snapshots needed for change detection.
