#!/usr/bin/env python3
"""
Синхронизация авто с пробегом galeonauto.com -> товары сообщества ВКонтакте.

  * нет в ВК            -> market.add
  * есть и изменилось   -> market.edit
  * нет на сайте        -> market.delete

Ключ сопоставления: полный VIN, записывается в поле "артикул" (sku) товара ВК.
Товары в ВК без sku скрипт не трогает.

Запуск:
  export VK_TOKEN="..."          # токен админа сообщества, права market + photos
  export VK_GROUP_ID="123456"    # id сообщества без минуса
  export VK_CATEGORY_ID="..."    # см. python3 galeon_vk_sync.py --categories

  python3 galeon_vk_sync.py --categories   # показать категории товаров ВК
  python3 galeon_vk_sync.py --dry-run      # только показать, что будет сделано
  python3 galeon_vk_sync.py                # выполнить
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests


def load_env():
    """Читает файл .env рядом со скриптом (строки вида KEY=value)."""
    path = Path(__file__).with_name(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


def env_int(name):
    """Число из переменной окружения; нецифровые значения (заглушки) считаются пустыми."""
    v = (os.environ.get(name) or "").strip()
    return int(v) if v.lstrip("-").isdigit() else 0


# ---------- настройки ----------
VK_TOKEN = os.environ.get("VK_TOKEN", "")
GROUP_ID = env_int("VK_GROUP_ID")
CATEGORY_ID = env_int("VK_CATEGORY_ID")
VK_V = "5.199"

SITE_URL = "https://locator-backend.tradedealer.ru/filter"
SITE_PARAMS = {
    "order": "price",
    "reversed": "false",
    "carType": "used",
    "_token": "yk5z2sCbhaG20PZI",
    "_tokenProduct": "rjYa4jWLEjXULsf1",
    "_version": "desktop",
}
SITE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://galeonauto.com",
    "Referer": "https://galeonauto.com/",
    "Accept": "application/json",
}
PAGE_LIMIT = 30
MAX_PHOTOS = 5  # ВК: главное фото + до 4 дополнительных
STATE_FILE = Path(__file__).with_name("state.json")

DRIVE = {"4wd": "Полный", "awd": "Полный", "fwd": "Передний", "rwd": "Задний"}


# ---------- сайт ----------
def fetch_site():
    cars = {}
    page = 1
    total = None
    while page <= 50:
        params = dict(SITE_PARAMS, page=page, limit=PAGE_LIMIT)
        r = requests.get(SITE_URL, params=params, headers=SITE_HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        total = data.get("total", total)
        items = data.get("list") or []
        if not items:
            break
        for c in items:
            key = c.get("vin_full") or c["id"]
            cars[key] = c
        if total is not None and len(cars) >= total:
            break
        page += 1
        time.sleep(0.5)
    return cars, total


def clean_comment(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\r", "")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def build_fields(c):
    m = c.get("modification") or {}
    eng = m.get("engine") or {}
    comp = c.get("company") or {}
    phones = (comp.get("phones") or {})

    specs = []

    def add(label, val):
        if val not in (None, "", 0):
            specs.append("%s: %s" % (label, val))

    add("Год выпуска", c.get("year"))
    if c.get("run"):
        add("Пробег", "{:,} км".format(int(c["run"])).replace(",", " "))
    eng_parts = []
    if eng.get("volume"):
        eng_parts.append("%g л" % eng["volume"])
    if eng.get("power"):
        eng_parts.append("%s л.с." % eng["power"])
    if (eng.get("fuel") or {}).get("title"):
        eng_parts.append(eng["fuel"]["title"])
    add("Двигатель", ", ".join(eng_parts))
    add("КПП", (m.get("transmission") or {}).get("title"))
    drive_alias = (m.get("drivetrainStructured") or {}).get("alias")
    add("Привод", DRIVE.get(drive_alias, drive_alias))
    add("Кузов", (m.get("body") or {}).get("title"))
    add("Цвет", (c.get("color") or {}).get("title"))
    add("Владельцев по ПТС", c.get("countOwners"))
    add("VIN", c.get("vin_full"))

    parts = ["\n".join(specs)]
    comment = clean_comment(c.get("comment"))
    if comment:
        parts.append(comment)
    contacts = []
    if comp.get("commerceTitle"):
        contacts.append(comp["commerceTitle"])
    if comp.get("address"):
        contacts.append("Москва, " + comp["address"])
    if phones.get("usedCars") or phones.get("main"):
        contacts.append("Тел.: " + (phones.get("usedCars") or phones.get("main")))
    if contacts:
        parts.append("\n".join(contacts))

    description = "\n\n".join(p for p in parts if p)[:14000]
    price = c.get("specialPrice") or c.get("price") or 0

    return {
        "name": (c.get("title") or "Автомобиль")[:100],
        "description": description,
        "price": int(price),
        "category_id": CATEGORY_ID,
    }


def photo_urls(c):
    large = ((c.get("sizesPhotos") or {}).get("large")) or []
    if not large:
        main = (c.get("photos") or {}).get("main")
        large = [main] if main else []
    return large[:MAX_PHOTOS]


def digest(obj):
    return hashlib.md5(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ---------- ВК ----------
def vk(method, **params):
    params.update(access_token=VK_TOKEN, v=VK_V)
    for attempt in range(5):
        r = requests.post("https://api.vk.com/method/" + method, data=params, timeout=60)
        j = r.json()
        time.sleep(0.4)
        if "error" in j:
            code = j["error"].get("error_code")
            if code in (6, 9) and attempt < 4:  # слишком часто / flood control
                time.sleep(2 + attempt * 2)
                continue
            raise RuntimeError("VK %s: %s" % (method, j["error"]))
        return j["response"]


def vk_market_items():
    items, offset = {}, 0
    no_sku = 0
    while True:
        r = vk("market.get", owner_id=-GROUP_ID, count=200, offset=offset)
        for it in r["items"]:
            sku = it.get("sku")
            if sku:
                items[sku] = it["id"]
            else:
                no_sku += 1
        offset += 200
        if offset >= r["count"]:
            break
    return items, no_sku


def upload_photo(url, main):
    img = requests.get(url, headers={"User-Agent": SITE_HEADERS["User-Agent"]}, timeout=60)
    img.raise_for_status()
    kw = {"group_id": GROUP_ID}
    if main:
        kw["main_photo"] = 1
    srv = vk("photos.getMarketUploadServer", **kw)
    up = requests.post(srv["upload_url"], files={"file": ("photo.jpg", img.content, "image/jpeg")}, timeout=120).json()
    save = dict(group_id=GROUP_ID, photo=up["photo"], server=up["server"], hash=up["hash"])
    if main:
        save["crop_data"] = up.get("crop_data", "")
        save["crop_hash"] = up.get("crop_hash", "")
    res = vk("photos.saveMarketPhoto", **save)
    return res[0]["id"]


def upload_all(urls):
    ids = [upload_photo(u, main=(i == 0)) for i, u in enumerate(urls)]
    return ids[0], ids[1:]


# ---------- состояние ----------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- основная логика ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="ничего не менять в ВК")
    ap.add_argument("--force", action="store_true", help="разрешить массовое удаление")
    ap.add_argument("--categories", action="store_true", help="показать категории товаров ВК")
    args = ap.parse_args()

    if not VK_TOKEN or not GROUP_ID:
        sys.exit("Задайте VK_TOKEN и VK_GROUP_ID")

    if args.categories:
        r = vk("market.getCategories", count=1000)
        for cat in r["items"]:
            print(cat["id"], "|", (cat.get("section") or {}).get("name"), "|", cat["name"])
        return
    if not CATEGORY_ID:
        sys.exit("Задайте VK_CATEGORY_ID (список: --categories)")

    site, total = fetch_site()
    print("На сайте: %d авто (total по API: %s)" % (len(site), total))
    if not site or (total is not None and len(site) != total):
        sys.exit("Выгрузка неполная или пустая - останавливаюсь, чтобы ничего не удалить по ошибке")

    vk_items, no_sku = vk_market_items()
    print("В ВК: %d товаров с sku (без sku, не трогаю: %d)" % (len(vk_items), no_sku))

    state = load_state()
    to_add = [k for k in site if k not in vk_items]
    to_delete = [k for k in vk_items if k not in site]
    to_check = [k for k in site if k in vk_items]

    if len(to_delete) > max(3, len(vk_items) // 2) and not args.force:
        sys.exit("Слишком много удалений (%d из %d). Проверьте парсер или запустите с --force."
                 % (len(to_delete), len(vk_items)))

    stats = {"add": 0, "edit": 0, "delete": 0, "same": 0}

    for vin in to_add:
        c = site[vin]
        fields = build_fields(c)
        urls = photo_urls(c)
        print("+ ДОБАВИТЬ  %s | %s | %s руб." % (vin, fields["name"], fields["price"]))
        if args.dry_run:
            continue
        if not urls:
            print("  нет фото, пропускаю (ВК требует главное фото)")
            continue
        main_id, extra_ids = upload_all(urls)
        res = vk("market.add", owner_id=-GROUP_ID, main_photo_id=main_id,
                 photo_ids=",".join(map(str, extra_ids)), sku=vin, **fields)
        vk_items[vin] = res["market_item_id"]
        state[vin] = {"fields": digest(fields), "photos": digest(urls)}
        save_state(state)
        stats["add"] += 1

    for vin in to_check:
        c = site[vin]
        fields = build_fields(c)
        urls = photo_urls(c)
        f_hash, p_hash = digest(fields), digest(urls)
        old = state.get(vin, {})
        fields_changed = old.get("fields") != f_hash
        photos_changed = ("photos" in old) and old["photos"] != p_hash
        if not fields_changed and not photos_changed:
            stats["same"] += 1
            continue
        what = []
        if fields_changed:
            what.append("поля")
        if photos_changed:
            what.append("фото")
        print("~ ИЗМЕНИТЬ  %s | %s | %s руб. (%s)" % (vin, fields["name"], fields["price"], ", ".join(what)))
        if args.dry_run:
            continue
        extra = {}
        if photos_changed and urls:
            main_id, extra_ids = upload_all(urls)
            extra = {"main_photo_id": main_id, "photo_ids": ",".join(map(str, extra_ids))}
        vk("market.edit", owner_id=-GROUP_ID, item_id=vk_items[vin], **dict(fields, **extra))
        state[vin] = {"fields": f_hash, "photos": p_hash if (photos_changed or "photos" not in old) else old["photos"]}
        save_state(state)
        stats["edit"] += 1

    for vin in to_delete:
        print("- УДАЛИТЬ    %s (item_id=%s)" % (vin, vk_items[vin]))
        if args.dry_run:
            continue
        vk("market.delete", owner_id=-GROUP_ID, item_id=vk_items[vin])
        state.pop(vin, None)
        save_state(state)
        stats["delete"] += 1

    if args.dry_run:
        print("\nDRY-RUN: добавить %d, проверить %d, удалить %d" % (len(to_add), len(to_check), len(to_delete)))
    else:
        print("\nГотово: добавлено %d, изменено %d, удалено %d, без изменений %d"
              % (stats["add"], stats["edit"], stats["delete"], stats["same"]))


if __name__ == "__main__":
    main()
