#!/usr/bin/env python3
"""
Собирает galeon.yml из авто на galeonauto.com для импорта в ВК.

Логика:
  * машина есть на сайте            -> available="true", данные свежие
  * машина пропала с сайта          -> остаётся в файле с available="false"
                                       (в ВК карточка станет недоступной)
                                       SOLD_KEEP_DAYS дней, потом выпадает из файла
  * машина вернулась на сайт        -> снова available="true"

Состояние хранится в yml_state.json рядом со скриптом.

  python3 make_yml_v2.py            обычный запуск
  python3 make_yml_v2.py --force    разрешить массовое "снятие с наличия"
"""
import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

from galeon_vk_sync import build_fields, fetch_site, photo_urls

OUT = Path(__file__).with_name("galeon.yml")
STATE = Path(__file__).with_name("yml_state.json")
CATEGORY_ID = "1"
CATEGORY_NAME = "Автомобили с пробегом"
SOLD_KEEP_DAYS = 30

BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean(s):
    return BAD_XML.sub("", s or "")


def offer_data(c):
    f = build_fields(c)
    return {
        "name": f["name"],
        "description": f["description"],
        "price": f["price"],
        "pics": photo_urls(c),
        "year": c.get("year"),
        "run": c.get("run"),
    }


def build_yml(items):
    """items: {vin: {"offer": {...}, "available": bool, ...}}"""
    root = ET.Element("yml_catalog", date=datetime.now().strftime("%Y-%m-%d %H:%M"))
    shop = ET.SubElement(root, "shop")
    ET.SubElement(shop, "name").text = "Галеон Авто"
    ET.SubElement(shop, "company").text = "Галеон Авто"
    cur = ET.SubElement(shop, "currencies")
    ET.SubElement(cur, "currency", id="RUR", rate="1")
    cats = ET.SubElement(shop, "categories")
    ET.SubElement(cats, "category", id=CATEGORY_ID).text = CATEGORY_NAME
    offers = ET.SubElement(shop, "offers")

    for vin, it in items.items():
        o = it["offer"]
        el = ET.SubElement(offers, "offer", id=str(vin), available="true" if it["available"] else "false")
        ET.SubElement(el, "name").text = clean(o["name"])
        ET.SubElement(el, "description").text = clean(o["description"])
        ET.SubElement(el, "price").text = str(o["price"])
        ET.SubElement(el, "currencyId").text = "RUR"
        ET.SubElement(el, "categoryId").text = CATEGORY_ID
        for p in o["pics"]:
            ET.SubElement(el, "picture").text = p
        # у ВК лимит: не более 2 свойств (param) на товар
        if o.get("year"):
            ET.SubElement(el, "param", name="Год выпуска").text = str(o["year"])
        if o.get("run") is not None:
            ET.SubElement(el, "param", name="Пробег, км").text = str(o["run"])

    ET.indent(root)
    return root


def load_prev():
    if not STATE.exists():
        return None
    data = json.loads(STATE.read_text(encoding="utf-8"))
    return data.get("cars")


def merge(prev, cur_offers, now):
    """Возвращает (новое состояние, отчёт)."""
    state = {}
    rep = {"new": [], "price": [], "sold": [], "back": [], "expired": [], "legacy": []}

    for vin, o in cur_offers.items():
        state[vin] = {"offer": o, "available": True, "sold_at": None}
        p = (prev or {}).get(vin)
        if p is None:
            if prev is not None:
                rep["new"].append(vin)
        else:
            old_offer = p.get("offer")
            if old_offer and old_offer.get("price") != o["price"]:
                rep["price"].append((vin, old_offer["price"], o["price"]))
            if p.get("available") is False:
                rep["back"].append(vin)

    for vin, p in (prev or {}).items():
        if vin in cur_offers:
            continue
        if "offer" not in p:  # состояние старого формата: восстановить карточку нечем
            rep["legacy"].append((vin, p.get("title", "")))
            continue
        if p.get("available", True):
            state[vin] = {"offer": p["offer"], "available": False, "sold_at": now.isoformat(timespec="seconds")}
            rep["sold"].append(vin)
        else:
            sold_at = datetime.fromisoformat(p["sold_at"]) if p.get("sold_at") else now
            if now - sold_at <= timedelta(days=SOLD_KEEP_DAYS):
                state[vin] = p
            else:
                rep["expired"].append(vin)
    return state, rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="разрешить массовое снятие с наличия")
    args = ap.parse_args()

    cars, total = fetch_site()
    print("Получено с сайта: %d авто (total: %s)" % (len(cars), total))
    if not cars or (total is not None and len(cars) != total):
        sys.exit("Выгрузка неполная или пустая, файл не меняю.")

    cur_offers, skipped = {}, []
    for vin, c in cars.items():
        o = offer_data(c)
        if not o["pics"] or not o["price"]:
            skipped.append(vin)  # без фото и цены ВК товар всё равно пропустит
            continue
        cur_offers[vin] = o
    if skipped:
        print("Пропущены (нет фото или цены):", ", ".join(skipped))

    now = datetime.now()
    prev = load_prev()
    state, rep = merge(prev, cur_offers, now)

    active_prev = len([1 for p in (prev or {}).values() if p.get("available", True)])
    if len(rep["sold"]) > max(5, active_prev // 2) and not args.force:
        sys.exit("Сразу %d из %d машин пропали с сайта. Похоже на сбой, файл не меняю. "
                 "Если это правда, запустите с --force." % (len(rep["sold"]), active_prev))

    ET.ElementTree(build_yml(state)).write(OUT, encoding="utf-8", xml_declaration=True)
    STATE.write_text(json.dumps({"updated": now.isoformat(timespec="seconds"), "cars": state},
                                ensure_ascii=False, indent=1), encoding="utf-8")

    n_active = len([1 for s in state.values() if s["available"]])
    print("Файл: %s (%.1f КБ) | в наличии: %d, недоступных: %d"
          % (OUT, OUT.stat().st_size / 1024, n_active, len(state) - n_active))

    if prev is None:
        print("Первый запуск: сравнивать пока не с чем, состояние сохранено.")
        return

    def title(vin):
        return state.get(vin, {}).get("offer", {}).get("name") or (prev.get(vin, {}).get("offer", {}) or {}).get("name") or vin

    print("\nНовых: %d | цена: %d | сняты с наличия: %d | вернулись: %d | выпали из файла: %d"
          % (len(rep["new"]), len(rep["price"]), len(rep["sold"]), len(rep["back"]), len(rep["expired"])))
    for vin in rep["new"]:
        print("  + новая: %s | %s руб." % (title(vin), state[vin]["offer"]["price"]))
    for vin, old, new in rep["price"]:
        print("  ~ цена: %s | %s -> %s руб." % (title(vin), old, new))
    for vin in rep["sold"]:
        print("  x нет на сайте (станет недоступной в ВК): %s" % title(vin))
    for vin in rep["back"]:
        print("  < вернулась: %s" % title(vin))
    if rep["expired"]:
        print("\nДАВНО НЕ В НАЛИЧИИ, больше не в файле (можно удалить в ВК вручную):")
        for vin in rep["expired"]:
            print("  - %s | VIN %s" % (title(vin), vin))
    if rep["legacy"]:
        print("\nУДАЛИТЕ В ВК ВРУЧНУЮ (старый формат состояния, восстановить нечем):")
        for vin, t in rep["legacy"]:
            print("  - %s | VIN %s" % (t, vin))


if __name__ == "__main__":
    main()
