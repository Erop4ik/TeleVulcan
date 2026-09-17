# TeleVulcan

> 🇬🇧 English version: [README.md](README.md)

Telegram-бот для нового дневника **VULCAN «Uczeń»** (`dziennik-uczen.vulcan.net.pl`).
Следит за планом занятий (замены, отмены, изменения), новыми оценками и домашними заданиями
и присылает только изменения.

## Как устроена привязка и почему нужен пароль

Vulcan для обычных аккаунтов (логин вида `ABCDEFG-123456`) **не выдаёт токенов** и не имеет OAuth
для сторонних приложений. Все куки сессионные и умирают примерно через 20 минут простоя, продлить
их без повторного ввода логина и пароля невозможно. Поэтому:

- пользователь вводит логин и пароль **не в Telegram**, а на странице `/link/<token>` сайта бота;
- бот проверяет данные реальным входом в дневник, шифрует их (Fernet, ключ `SECRET_KEY`) и хранит в SQLite;
- при каждом опросе бот заходит в дневник заново; `/unlink` удаляет всё.

Это честно написано на странице привязки, галочка согласия обязательна.

## Как бот логинится

1. `GET dziennik-uczen/<symbol>/Account/Login` → редирект на `dziennik-logowanie/<symbol>/Account/Logon`.
2. Форма `Login`, `Haslo`, `__RequestVerificationToken`, `captcha-response`.
   «Капча» — proof-of-work из `captcha.js`: 15 раундов SHA-256, ищется nonce, при котором первые
   4 байта хэша `< difficulty`. Решается за ~0.7 с (`vulcanbot/vulcan/pow.py`).
3. Ответ — WS-Federation авто-форма (`wa`, `wresult`, `wctx`), которую клиент сам постит на
   `dziennik-uczen/.../Account/Login`; после этого появляется кука `Dziennik.Uczen.Sso`.
4. `key` ученика берётся из `GET api/Context` (`uczniowie[0].key`), см. раздел «API».
5. API: `GET /<symbol>/api/<Name>?key=<key>&...`. При 401/409/редиректе клиент перелогинивается.

## Установка

```bash
pip install -r requirements.txt
cp .env.example .env   # заполнить BOT_TOKEN, PUBLIC_URL, SECRET_KEY, VULCAN_SYMBOL
python -m vulcanbot
```

`SECRET_KEY`:

```bash
python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
```

Сайт привязки слушает `WEB_PORT` (по умолчанию 8080). Наружу его нужно выставить **только по HTTPS**
(nginx/Caddy/Cloudflare Tunnel), `PUBLIC_URL` — адрес, который увидят пользователи.

## Интерфейс

Бот использует **Rich Messages** (Bot API 10.x, aiogram ≥ 3.31): заголовки, таблицы, списки,
плюс инлайн-кнопки для навигации. Rich-сообщения нельзя редактировать, поэтому листание — это
удаление старого сообщения и отправка нового. Если клиент Telegram старый и rich-сообщение
не принято, бот шлёт обычный HTML-текст.

Внизу постоянная клавиатура: **📅 План · 📝 ДЗ · 🎓 Оценки · ℹ️ Статус**.

| Экран | Навигация |
|---|---|
| План | кнопки дней Пн…Пт (⚠️ = в этот день есть замены), ◀ неделя / вся неделя / неделя ▶ |
| ДЗ | ◀ неделя / сегодня / неделя ▶ |
| Оценки | предметы с оценками, переключение semestr 1/2, «все» |

Команды: `/start`, `/link`, `/plan [next]`, `/hw [next]`, `/grades`, `/status`, `/unlink`.

Сессии Vulcan живут в пуле (`vulcanbot/pool.py`) до 15 минут простоя, поэтому нажатия кнопок
не вызывают новый логин с PoW каждый раз.

## API (снято зондом 17.09.2026)

Все запросы: `GET https://dziennik-uczen.vulcan.net.pl/<symbol>/api/<Name>?key=<key>&...`,
даты в ISO UTC, границы недели по Europe/Warsaw (`2026-09-13T22:00:00.000Z`…`2026-09-20T21:59:59.999Z`).

| Эндпоинт | Параметры | Ответ | Статус |
|---|---|---|---|
| `Context` | без key | `{uczniowie:[{key, uczen, oddzial, jednostka, idDziennik, config{...}}]}` | подтверждён, отсюда берётся `key` |
| `PlanZajec` | `dataOd, dataDo, zakresDanych=2` | `[{data, godzinaOd, godzinaDo, przedmiot, prowadzacy, sala, podzial, zmiany[], zmianyUwagi[], adnotacja, dodatkowe, zrealizowane}]` | подтверждён |
| `SprawdzianyZadaniaDomowe` | `dataOd, dataDo` | `[{typ, przedmiotNazwa, data, hasAttachment, id}]`; typ 1 sprawdzian, 2 kartkówka, 3 praca klasowa (?), 4 zadanie domowe | подтверждён |
| `SprawdzianSzczegoly` | `id` (typ 1–3) | `{typ, data, przedmiotNazwa, nauczycielImieNazwisko, opis, linki[], id}` | подтверждён |
| `ZadanieDomoweSzczegoly` | `id` (typ 4) | аналогично; с id проверочной даёт 500 | подтверждён |
| `Frekwencja` | `dataOd, dataDo` | `{oddzialy:[{numerLekcji, kategoriaFrekwencji, data, opisZajec, nauczyciel}], ...}` | подтверждён |
| `Uwagi` | — | `[]` | подтверждён |
| `OkresyKlasyfikacyjne` | `idDziennik` | `[{numerOkresu, dataOd, dataDo, id}]` | подтверждён |
| `Oceny` | `idDziennik, idOkresKlasyfikacyjny` | `{ocenyPrzedmioty:[{przedmiotNazwa, kolumnyOcenyCzastkowe:[{kategoriaKolumny, nazwaKolumny, oceny:[{wpis, dataOceny, waga, nauczyciel, kolorOceny, idKolumny, idOcenaPoprawiona}]}], srednia, ocenaOkresowa, proponowanaOcenaOkresowa}], ustawienia}` | подтверждён |

`idDziennik` берётся из `Context.uczniowie[].idDziennik`. `key` — двойной base64 от `schoolId-studentId-x-y` (`TVRJ…` → `MTIz…` → `12345-67890-1-17`).

Замена учителя в `PlanZajec` выглядит так:

```json
"zmiany": [{"zmiana": 7, "typProwadzacego": 1, "prowadzacy": "Kowalska Anna", "sala": "", ...}], "adnotacja": 1
```

Имена контроллеров вытащены из бандла SPA (`prodbundle_*.js`, статическое поле `ApiName` и
имена view). Полный список кандидатов в `tools/probe.py`. Зонд:

```bash
python tools/probe.py
```

## Ограничения и этика

- Неофициальный клиент. Vulcan может поменять форму логина или API в любой момент.
- Не опрашивай чаще, чем раз в несколько минут (`POLL_INTERVAL`), чтобы не словить блокировку аккаунта.
- Не запускай публичный инстанс без HTTPS и без понимания, что ты хранишь чужие пароли.
- Бот не запрашивает `DaneUcznia` и `Konto` (там дата рождения, email и т.п.) и не хранит ничего, кроме снимков плана, ДЗ и оценок для сравнения.
