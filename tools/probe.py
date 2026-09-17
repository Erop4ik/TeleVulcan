"""Зонд: логинится в дневник и сохраняет ответы API в probe_out/ — чтобы увидеть
реальные схемы (план, оценки, ДЗ) и подобрать имена эндпоинтов.

Запуск (логин/пароль спрашиваются в консоли, никуда не сохраняются):
    python tools/probe.py
    python tools/probe.py Oceny ZadaniaDomowe Frekwencja   # свои кандидаты эндпоинтов
"""
import asyncio
import getpass
import logging
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vulcanbot.vulcan.client import VulcanClient, VulcanError, decode_key, week_range  # noqa: E402

GRADE_PARAM_NAMES = ("idOkresKlasyfikacyjny", "okresKlasyfikacyjnyId", "idOkres", "okresId", "idOkresu")

CANDIDATES = []


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    symbol = os.environ.get("VULCAN_SYMBOL") or input("Символ (poznan): ").strip() or "poznan"
    login = input("Логин: ").strip()
    password = getpass.getpass("Пароль: ")
    names = sys.argv[1:] or CANDIDATES
    out = Path("probe_out")
    out.mkdir(exist_ok=True)
    os.environ.setdefault("VULCAN_DEBUG_DIR", str(out))

    async with VulcanClient(symbol, login, password) as c:
        await c.login_flow()
        print("key =", c.key, decode_key(c.key or ""))
        if (out / "Context.json").exists():
            print("Context: сохранил probe_out/Context.json")
        od, do = week_range()
        plan = await c.plan()
        (out / "PlanZajec.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), "utf-8")
        print("PlanZajec: ok, сохранил probe_out/PlanZajec.json")
        # idDziennik из Context — часть сервисов шлёт {key, idDziennik}
        ctx = json.loads((out / "Context.json").read_text("utf-8")) if (out / "Context.json").exists() else {}
        id_dz = next((u.get("idDziennik") for u in ctx.get("uczniowie", []) if u.get("key") == c.key), None)
        print("idDziennik =", id_dz)
        okresy = await c.api("OkresyKlasyfikacyjne", idDziennik=id_dz)
        (out / "OkresyKlasyfikacyjne.json").write_text(json.dumps(okresy, ensure_ascii=False, indent=1), "utf-8")
        okres_id = next((o["id"] for o in okresy if o.get("numerOkresu") == 1), okresy[0]["id"])
        print("okres id =", okres_id)
        combos = []
        for pname in ("idOkresKlasyfikacyjny", "okresKlasyfikacyjnyId", "idOkres", "okresId", "idOkresu", "okres", "idOkresKlasyfikacyjnego"):
            combos.append({"idDziennik": id_dz, pname: okres_id})
            combos.append({pname: okres_id})
        combos.append({"idDziennik": id_dz, "dataOd": od, "dataDo": do})
        found = False
        for ep in ("Oceny", "OcenyBiezace", "OcenyOkresowe", "OcenyZZachowania", "OcenyOpisowe", "OcenyPunktowe",
                   "OcenyOkresoweWykres", "PrzedmiotyUczniaOcenyWykresy"):
            for params in combos:
                try:
                    data = await c.api(ep, **params)
                except VulcanError as e:
                    code = str(e).split("HTTP ")[1][:3] if "HTTP " in str(e) else "?"
                    if code != "404":
                        print(f"{ep} {params}: {str(e)[:90]}")
                    continue
                if isinstance(data, str):
                    continue
                (out / f"{ep}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
                print(f"{ep} {params}: OK -> probe_out/{ep}.json  {json.dumps(data, ensure_ascii=False)[:300]}")
                found = True
                break
            if found:
                break
        if not found:
            print("ОЦЕНКИ: ни одна комбинация не дала JSON (все 404/HTML)")
        # детали ДЗ: POST-варианты (SPA шлёт anti-forgery заголовки только на POST)
        hw = await c.homework()
        if hw:
            for body in ({"id": hw[0]["id"]}, {"id": hw[0]["id"], "idDziennik": id_dz},
                         {"idZadanieDomowe": hw[0]["id"]}, {"zadanieDomoweId": hw[0]["id"]}):
                for ep in ("ZadanieDomoweSzczegoly", "ZadanieDomowe", "SprawdzianyZadaniaDomowe/Szczegoly"):
                    try:
                        det = await c.api_post(ep, **body)
                    except VulcanError as e:
                        print(f"POST {ep} {body}: {str(e)[:70]}"); continue
                    if isinstance(det, str):
                        print(f"POST {ep} {body}: не JSON"); continue
                    (out / "ZadanieDomoweSzczegoly_POST.json").write_text(json.dumps(det, ensure_ascii=False, indent=1), "utf-8")
                    print(f"POST {ep} {body}: OK -> {json.dumps(det, ensure_ascii=False)[:300]}")
        # детали первого ДЗ (GET-варианты)
        if hw:
            item = hw[0]
            for extra in ({}, {"idDziennik": id_dz}, {"idDziennik": id_dz, "idOkresKlasyfikacyjny": okres_id},
                          {"typ": item["typ"]}, {"idDziennik": id_dz, "typ": item["typ"]},
                          {"dataOd": od, "dataDo": do}, {"idDziennik": id_dz, "dataOd": od, "dataDo": do}):
                try:
                    det = await c.api("ZadanieDomoweSzczegoly", id=item["id"], **extra)
                except VulcanError as e:
                    print(f"ZadanieDomoweSzczegoly id={item['id']} {extra}: {str(e)[:60]}"); continue
                if isinstance(det, str):
                    print(f"ZadanieDomoweSzczegoly {extra}: не JSON"); continue
                (out / "ZadanieDomoweSzczegoly.json").write_text(json.dumps(det, ensure_ascii=False, indent=1), "utf-8")
                print(f"ZadanieDomoweSzczegoly id={item['id']} {extra}: OK -> {json.dumps(det, ensure_ascii=False)[:300]}")
                break
        for name in names:
            for params in ({}, {"dataOd": od, "dataDo": do}, {"dataOd": od, "dataDo": do, "zakresDanych": 2}):
                try:
                    data = await c.api(name, **params)
                except VulcanError as e:
                    print(f"{name} {params or ''}: {str(e)[:90]}")
                    continue
                if isinstance(data, str):
                    print(f"{name} {params or ''}: не JSON ({len(data)} байт)"); break
                (out / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
                print(f"{name} {params or ''}: OK -> probe_out/{name}.json")
                break


def _walk_ints(obj, keys):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, int):
                yield v
            yield from _walk_ints(v, keys)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_ints(v, keys)


if __name__ == "__main__":
    asyncio.run(main())
