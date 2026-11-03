"""Щоденна «Хвилина мовчання» АКМЦ: історія загиблого з Донеччини чи Луганщини о 9:00.

Режими запуску:
  python bot.py auto     — звичайний щоденний режим (так запускає GitHub Actions)
  python bot.py test     — обрати людину й надіслати пост лише на перевірку, нічого не зберігаючи
  python bot.py now      — обрати людину й одразу опублікувати в CHANNEL_ID (для тестів у внутрішньому чаті)
  python bot.py index    — оновити список людей (index)
  python bot.py index --full — повністю перебудувати список
"""
import json
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import card
import memorial
from memorial import (CATEGORIES, birth_city_options, fetch, get_person, list_url,
                      parse_date, parse_list)
from post import build_post, city_line_city
from regions import (BIRTH_CITIES, DEATH_OBLAST_CODES, DONETSK, LUHANSK, find_regions,
                     normalize, origin_regions, region_from_field)

KYIV = ZoneInfo("Europe/Kyiv")
STATE = Path(__file__).parent / "state"
INDEX_FILE = STATE / "index.json"
PUBLISHED_FILE = STATE / "published.json"
PENDING_FILE = STATE / "pending.json"

# ---------- графік ----------
# Пн=0 … Нд=6
WEEKDAY_CATEGORY = {0: "civilians", 1: "militaries", 2: "civilians", 3: "militaries",
                    4: "civilians", 5: "militaries", 6: "children"}
LUHANSK_DAYS = {1, 4}  # вівторок і пʼятниця


def env(name, default=None):
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) else v


def load(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save(path, data):
    STATE.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def log(*a):
    print(datetime.now(KYIV).strftime("%H:%M:%S"), *a, flush=True)


# ---------- індекс людей ----------
# index = {"seen": {cat: [ids]}, "death": {region: [ids]}, "people": {id: card}}
# У "people" зберігаємо лише тих, хто схожий на людину з наших областей.

def card_regions(card, death_index, birth_index=None):
    """Попередня оцінка за карткою у списку. Остаточно перевіряє verify()."""
    born = {reg for reg, ids in (birth_index or {}).items() if card["id"] in ids}
    if card["cat"] == "militaries":
        # для військових — лише походження («Родом з Краматорська», місто народження), не місце бою
        return sorted(origin_regions(card["desc"]) | born)
    text = " ".join([card["desc"], " ".join(card["fields"].values())])
    regs = find_regions(text) | born
    for reg, ids in death_index.items():
        if card["id"] in ids:
            regs.add(reg)
    return sorted(regs)


def crawl(category, index_seen, full, oblast_code=None, birth_city_id=None):
    """Проходить сторінки списку від найновіших. Зупиняється, коли сторінка не дає нічого нового."""
    page, last, new_cards = 1, 1, []
    while page <= last:
        cards, last = parse_list(fetch(list_url(category, page, oblast_code, birth_city_id)), category)
        fresh = [c for c in cards if c["id"] not in index_seen]
        new_cards.extend(cards if (oblast_code or birth_city_id) else fresh)
        index_seen.update(c["id"] for c in cards)
        tag = f"обл.{oblast_code}" if oblast_code else (f"нар.{birth_city_id}" if birth_city_id else "")
        log(f"  {category} {tag} стор. {page}/{last}: нових {len(fresh)}")
        if not full and not fresh:
            break
        page += 1
    return new_cards


def update_index(full=False):
    index = load(INDEX_FILE, {"seen": {}, "death": {}, "people": {}})
    if full:
        index = {"seen": {}, "death": {}, "people": {}}
    log("Оновлюю список людей", "(повністю)" if full else "(лише нові)")

    # хто загинув на території області (для цивільних і дітей)
    for reg, code in DEATH_OBLAST_CODES.items():
        ids = set(index["death"].get(reg, []))
        for cat in ("civilians", "children"):
            seen_key = f"death-{reg}-{cat}"
            seen = set(index["seen"].get(seen_key, []))
            for c in crawl(cat, seen, full, code):
                ids.add(c["id"])
            index["seen"][seen_key] = sorted(seen)
        index["death"][reg] = sorted(ids)

    # хто народився в містах області (фільтр «Місто народження»).
    # ~300 запитів, тому лише раз на тиждень (у неділю), при повному оновленні або якщо ще не робили
    index.setdefault("birth", {})
    weekly = full or not index["birth"] or datetime.now(KYIV).weekday() == 6
    options = birth_city_options(fetch(list_url("militaries", 1))) if weekly else {}
    for reg, cities in (BIRTH_CITIES.items() if weekly else []):
        ids = set(index["birth"].get(reg, []))
        for name in cities:
            for city_id in options.get(normalize(name), []):
                for cat in CATEGORIES:
                    seen_key = f"birth-{city_id}-{cat}"
                    seen = set(index["seen"].get(seen_key, []))
                    for c in crawl(cat, seen, full, birth_city_id=city_id):
                        ids.add(c["id"])
                        index["people"].setdefault(c["id"], c)
                    index["seen"][seen_key] = sorted(seen)
        index["birth"][reg] = sorted(ids)

    death_index = {r: set(v) for r, v in index["death"].items()}
    birth_index = {r: set(v) for r, v in index["birth"].items()}
    for cat in CATEGORIES:
        seen = set(index["seen"].get(cat, []))
        for card in crawl(cat, seen, full):
            if card_regions(card, death_index, birth_index):
                index["people"][card["id"]] = card
        index["seen"][cat] = sorted(seen)

    # перерахувати регіони для всіх збережених карток
    for pid, card in list(index["people"].items()):
        card["regions"] = card_regions(card, death_index, birth_index)
        if not card["regions"]:
            del index["people"][pid]

    save(INDEX_FILE, index)
    counts = {}
    for c in index["people"].values():
        for r in c["regions"]:
            counts[(c["cat"], r)] = counts.get((c["cat"], r), 0) + 1
    log("Людей у списку:", {f"{k[0]}/{k[1]}": v for k, v in sorted(counts.items())})
    return index


# ---------- відбір ----------

def verify(card):
    """Відкриває сторінку людини й перевіряє: наша область, є фото і текст.
    Повертає (Person, region) або (None, причина)."""
    try:
        person = get_person(card["url"], card["cat"])
    except Exception as e:  # сайт недоступний тощо
        return None, f"помилка завантаження: {e}"
    if not person.photo:
        return None, "немає фото"
    if len(person.story) < 2 and sum(len(p) for p in person.story) < 300:
        return None, "закороткий текст"

    region = region_from_field(person.f("Місто народження"))
    if not region:
        origin = origin_regions(person.desc) or origin_regions(" ".join(person.story[:2]))
        if len(origin) == 1:
            region = origin.pop()
    if not region and person.cat != "militaries":
        region = region_from_field(person.f("Область загибелі"))
    if not region:
        return None, "не підтверджено, що людина з Донеччини чи Луганщини"
    return person, region


def today_plan(day: date):
    cat = WEEKDAY_CATEGORY[day.weekday()]
    region = LUHANSK if day.weekday() in LUHANSK_DAYS else DONETSK
    return cat, region


def choose(index, published, rejected, day: date, rng: random.Random):
    used = set(published) | set(rejected)
    people = [c for pid, c in index["people"].items() if pid not in used]
    cat, region = today_plan(day)

    # 1) річниця загибелі має пріоритет
    anniv = []
    for c in people:
        d = parse_date(c["fields"].get("Дата загибелі", ""))
        if d and d[1] == day.month and d[2] == day.day and d[0] < day.year:
            anniv.append(c)
    # 2) за графіком, 3) запасні варіанти
    order = [anniv,
             [c for c in people if c["cat"] == cat and region in c["regions"]],
             [c for c in people if c["cat"] == cat],
             [c for c in people if c["cat"] in ("civilians", "militaries")]]
    if cat == "children":
        order.insert(2, [c for c in people if c["cat"] == "civilians"])

    tried = set()
    for pool in order:
        pool = [c for c in pool if c["id"] not in tried]
        rng.shuffle(pool)
        for card in pool[:15]:
            tried.add(card["id"])
            person, region_or_reason = verify(card)
            if person:
                reason = "річниця загибелі" if card in anniv else f"графік: {cat}"
                log(f"Обрано: {person.name} ({person.cat}, {region_or_reason}) — {reason}")
                return person, region_or_reason
            log(f"Пропускаю {card['name']}: {region_or_reason}")
            if "помилка" not in region_or_reason:
                rejected.append(card["id"])
    return None, None


# ---------- Telegram ----------

def tg(method, **params):
    token = env("TELEGRAM_BOT_TOKEN")
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=60)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data["result"]


def make_card(person, region):
    return card.card_for_person(person, city_line_city(person, region))


def tg_send(chat_id, text, card_bytes, silent=True):
    """Картка + текст. Підпис до фото в Telegram — до 1024 символів;
    довший текст іде окремим повідомленням одразу під карткою."""
    token = env("TELEGRAM_BOT_TOKEN")
    data = {"chat_id": chat_id, "disable_notification": str(silent).lower()}
    long_text = len(text) > 1024
    if not long_text:
        data.update(caption=text, parse_mode="HTML")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", data=data,
                      files={"photo": ("card.jpg", card_bytes, "image/jpeg")}, timeout=120).json()
    if not r.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto: {r}")
    if long_text:
        tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
           disable_notification=silent, link_preview_options={"is_disabled": True})
    return r["result"]


def stop_requested(since_ts: int) -> bool:
    """Чи написала редакторка боту «стоп» після надсилання чернетки."""
    review = str(env("REVIEW_CHAT_ID"))
    try:
        updates = tg("getUpdates", timeout=0, allowed_updates=["message"])
    except Exception as e:
        log("Не вдалося перевірити «стоп»:", e)
        return False
    stop = False
    for u in updates:
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) == review and m.get("date", 0) >= since_ts:
            if (m.get("text") or "").strip().lower() in ("стоп", "stop", "/stop"):
                stop = True
    if updates:
        tg("getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0)
    return stop


# ---------- Facebook ----------

def facebook_publish(person, region):
    page_id, token = env("FB_PAGE_ID"), env("FB_PAGE_TOKEN")
    if not (page_id and token):
        return None
    text = build_post(person, region, html_mode=False, network="facebook")
    r = requests.post(f"https://graph.facebook.com/v21.0/{page_id}/photos",
                      data={"caption": text, "access_token": token},
                      files={"source": ("card.jpg", make_card(person, region), "image/jpeg")},
                      timeout=120)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Facebook: {data['error']}")
    return data


# ---------- сценарії ----------

def prepare(day: date, dry_run=False):
    index = load(INDEX_FILE, None)
    if not index or not index.get("people"):
        index = update_index(full=True)
    else:
        index = update_index(full=False)
    published = load(PUBLISHED_FILE, {"rejected": [], "log": []})
    rng = random.Random()
    channel = env("CHANNEL_ID")
    person, region = choose(index, [p["id"] for p in published["log"] if p.get("channel") == channel],
                            published["rejected"], day, rng)
    if not dry_run:
        save(PUBLISHED_FILE, published)  # зберігаємо відхилених
    if not person:
        raise RuntimeError("Не знайдено жодної людини для поста")
    text = build_post(person, region)
    note = ("👆 Чернетка «Хвилини мовчання». "
            + ("Це тест, у канал не піде." if dry_run else
               f"Вийде о 9:00 {day.strftime('%d.%m')}. Щоб скасувати — напиши мені «стоп»."))
    tg_send(env("REVIEW_CHAT_ID"), text, make_card(person, region), silent=False)
    tg("sendMessage", chat_id=env("REVIEW_CHAT_ID"), text=note)
    if not dry_run:
        save(PENDING_FILE, {"date": day.isoformat(), "person": person.to_dict(),
                            "region": region, "preview_ts": int(time.time()),
                            "published": False})
    return person, region


def publish(pending):
    person = memorial.Person(**pending["person"])
    region = pending["region"]
    if stop_requested(pending["preview_ts"]):
        log("Редакторка написала «стоп» — сьогодні не публікуємо.")
        tg("sendMessage", chat_id=env("REVIEW_CHAT_ID"), text="Добре, сьогоднішній пост скасовано.")
        pending["published"] = "cancelled"
        save(PENDING_FILE, pending)
        return

    to_channel = env("PUBLISH_TO_CHANNEL", "false").lower() == "true"
    target = env("CHANNEL_ID") if to_channel else env("REVIEW_CHAT_ID")
    tg_send(target, build_post(person, region), make_card(person, region), silent=True)
    log("Telegram: опубліковано в", "канал" if to_channel else "чат перевірки (тестовий режим)")

    if to_channel:
        try:
            if facebook_publish(person, region):
                log("Facebook: опубліковано")
        except Exception as e:
            log("Facebook не вдалося:", e)
            tg("sendMessage", chat_id=env("REVIEW_CHAT_ID"), text=f"⚠️ Facebook: {e}")

    published = load(PUBLISHED_FILE, {"rejected": [], "log": []})
    if to_channel:
        published["log"].append({"id": person.id, "name": person.name, "cat": person.cat,
                                 "region": region, "date": pending["date"], "url": person.url,
                                 "channel": env("CHANNEL_ID")})
    save(PUBLISHED_FILE, published)
    pending["published"] = True
    save(PENDING_FILE, pending)


def auto():
    now = datetime.now(KYIV)
    today = now.date()
    pending = load(PENDING_FILE, {})

    if pending.get("date") != today.isoformat():
        log("Готую пост на сьогодні")
        prepare(today)
        pending = load(PENDING_FILE, {})

    start = now.replace(hour=8, minute=0, second=0, microsecond=0)
    end = now.replace(hour=10, minute=0, second=0, microsecond=0)
    now = datetime.now(KYIV)
    if pending.get("published") or not (start <= now < end):
        log("Публікувати зараз не час або вже опубліковано.")
        return
    nine = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now < nine:
        wait = (nine - now).total_seconds()
        log(f"Чекаю до 9:00 ({int(wait // 60)} хв)")
        time.sleep(wait)
    publish(pending)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if mode == "index":
        update_index(full="--full" in sys.argv)
    elif mode == "test":
        prepare(datetime.now(KYIV).date(), dry_run=True)
    elif mode == "now":
        # тест: обрати людину й одразу опублікувати в CHANNEL_ID, без очікування 9:00
        prepare(datetime.now(KYIV).date())
        pending = load(PENDING_FILE, {})
        pending["preview_ts"] = int(time.time())
        publish(pending)
    elif mode == "auto":
        auto()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
