"""Щоденна «Хвилина мовчання» АКМЦ: історія загиблого з Донеччини чи Луганщини о 9:00.

Режими запуску:
  python bot.py auto     — звичайний щоденний режим (так запускає GitHub Actions)
  python bot.py test     — обрати людину й надіслати пост лише на перевірку, нічого не зберігаючи
  python bot.py now      — обрати людину й одразу опублікувати в CHANNEL_ID (для тестів у внутрішньому чаті)
  python bot.py index    — оновити список людей (index)
  python bot.py index --full — повністю перебудувати список
"""
import io
import json
import os
import random
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image

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


def update_index(full=False, weekly=False):
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
    # ~700 запитів (~18 хв), тому лише в окремому недільному вечірньому запуску (birthscan),
    # при повному оновленні або якщо ще не робили. Не перед чернеткою і не на «Інша людина» (22.11).
    index.setdefault("birth", {})
    weekly = weekly or full or not index["birth"]
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
    cat = env("FORCE_CATEGORY") or WEEKDAY_CATEGORY[day.weekday()]   # FORCE_CATEGORY — для ручних тестів
    region = LUHANSK if day.weekday() in LUHANSK_DAYS else DONETSK
    return cat, region


def choose(index, published, rejected, day: date, rng: random.Random):
    used = set(published) | set(rejected)
    people = [c for pid, c in index["people"].items() if pid not in used]
    cat, region = today_plan(day)

    # 1) річниця загибелі має пріоритет (крім ручного вибору категорії)
    anniv = []
    forced = bool(env("FORCE_CATEGORY"))
    for c in people:
        d = parse_date(c["fields"].get("Дата загибелі", ""))
        if not forced and d and d[1] == day.month and d[2] == day.day and d[0] < day.year:
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

def review_token():
    """Бот для чернеток і кнопок (окремий «меморіальний» бот, якщо є)."""
    return env("MEMORIAL_BOT_TOKEN") or env("TELEGRAM_BOT_TOKEN")


def tg(method, _token=None, **params):
    token = _token or env("TELEGRAM_BOT_TOKEN")
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=60)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data["result"]


def make_card(person, region, nocut=False, contour=None, erase=None):
    return card.card_for_person(person, city_line_city(person, region), nocut=nocut, contour=contour,
                                erase=erase)


CONTOUR_PAGE = "https://anti-crisis-media-center.github.io/memorial-bot/contour.html"
CONTOUR_VERSION = "2"   # збільшувати після кожної зміни contour.html


def contour_keyboard(person):
    """Кнопка під полем вводу: відкриває сторінку, де редакторка обводить людину пальцем."""
    from urllib.parse import urlencode
    # v — щоб Telegram не показував стару збережену версію сторінки після оновлень
    url = CONTOUR_PAGE + "?" + urlencode({"img": person.photo, "id": person.id, "v": CONTOUR_VERSION})
    return {"keyboard": [[{"text": "✏️ Обвести контур", "web_app": {"url": url}}]],
            "resize_keyboard": True, "is_persistent": True}


def offer_contour(person):
    review_send("Якщо людину вирізано погано — натисни ✏️ «Обвести контур» під полем повідомлення. "
                "Там можна обвести людину пальцем, а якщо лишилось щось зайве (чуже плече, предмет) — "
                "перемкнись на 🧽 «Стерти зайве» і просто зафарбуй цей шматок.",
                keyboard=contour_keyboard(person))


TEST_GROUP_ID = "-1003946430136"  # «Тестування ботів АКМЦ» (супергрупа з гілками)
TEST_THREAD = "408"                # гілка «Меморіал» (з 25.11.2026; раніше «Тести» = 6)
MIGRATED_CHATS = {"-5294316060": TEST_GROUP_ID}   # «Тестування бота-новинаря» → супергрупа (листопад 2026)


CARDS_BRANCH = "cards"     # службова гілка з картками для прев'ю; тримаємо лише останні 60 днів
CARDS_KEEP_DAYS = 60


_HOSTED = {}


def host_card(card_bytes):
    """Те саме, що _host_card, але одну й ту саму картку викладаємо лише раз (чернетка йде кільком людям)."""
    import hashlib
    key = hashlib.sha1(card_bytes).hexdigest()
    if key not in _HOSTED:
        _HOSTED[key] = _host_card(card_bytes)
    return _HOSTED[key]


def _host_card(card_bytes):
    """Кладе картку в службову гілку і повертає публічну адресу (для прев'ю над текстом).
    Гілка щоразу перезаписується одним комітом без історії, тож репозиторій не росте:
    старі картки (понад 60 днів) просто випадають. Журнал опублікованих (state/) це не зачіпає.
    Повертає None, якщо не вдалося — тоді бот шле по-старому: картка й текст окремо."""
    import secrets, tempfile
    def git(*a, inp=None):
        r = subprocess.run(["git", *a], input=inp, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"git {a[0]}: {r.stderr.strip()[:300]}")
        return r.stdout.strip()
    try:
        now = datetime.now(KYIV)
        name = f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}.jpg"
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(card_bytes)
        blob = git("hash-object", "-w", f.name)
        entries = []
        if subprocess.run(["git", "fetch", "-q", "origin", CARDS_BRANCH], capture_output=True).returncode == 0:
            cutoff = (now - timedelta(days=CARDS_KEEP_DAYS)).strftime("%Y%m%d")
            for line in git("ls-tree", "FETCH_HEAD").splitlines():
                if line.split("\t", 1)[-1][:8] >= cutoff:
                    entries.append(line)
        entries.append(f"100644 blob {blob}\t{name}")
        tree = git("mktree", inp="\n".join(entries) + "\n")
        commit = git("-c", "user.name=memorial-bot", "-c", "user.email=memorial-bot@users.noreply.github.com",
                     "commit-tree", tree, "-m", f"Картки для прев'ю (останні {CARDS_KEEP_DAYS} днів)")
        git("push", "-q", "-f", "origin", f"{commit}:refs/heads/{CARDS_BRANCH}")
        repo = os.environ.get("GITHUB_REPOSITORY", "Anti-Crisis-Media-Center/memorial-bot")
        time.sleep(3)
        return f"https://raw.githubusercontent.com/{repo}/{CARDS_BRANCH}/{name}"
    except Exception as e:
        log("Не вдалося викласти картку для прев'ю:", e)
        return None


def tg_send(chat_id, text, card_bytes, silent=True, token=None, keyboard=None):
    """Картка + текст. Підпис до фото в Telegram — до 1024 символів;
    довший текст іде окремим повідомленням одразу під карткою.
    keyboard — кнопки під останнім повідомленням (для чернеток)."""
    token = token or env("TELEGRAM_BOT_TOKEN")
    # стара тестова група стала супергрупою з гілками — шлемо одразу за новою адресою, у гілку «Тести»
    chat_id = MIGRATED_CHATS.get(str(chat_id), chat_id)
    data = {"chat_id": chat_id, "disable_notification": str(silent).lower()}
    if str(chat_id) == TEST_GROUP_ID:
        data["message_thread_id"] = TEST_THREAD
    if keyboard and len(text) <= 1024:
        data["reply_markup"] = json.dumps(keyboard)
    long_text = len(text) > 1024
    if long_text:
        # Довгий текст: один допис — картка великим прев'ю над текстом (до 4096 символів)
        url = host_card(card_bytes)
        if url:
            extra = {"reply_markup": keyboard} if keyboard else {}
            if str(chat_id) == TEST_GROUP_ID:
                extra["message_thread_id"] = TEST_THREAD
            try:
                # Невидиме посилання на картку — першим у тексті. Без нього при ручному редагуванні
                # допису в Telegram картка зникала: застосунок бере прев'ю з першого посилання в тексті (20.11).
                text = f'<a href="{url}">\u2060</a>' + text
                return tg("sendMessage", _token=token, chat_id=chat_id, text=text, parse_mode="HTML",
                          disable_notification=silent, **extra,
                          link_preview_options={"url": url, "prefer_large_media": True, "show_above_text": True})
            except RuntimeError as e:
                if "chat not found" in str(e) or "Forbidden" in str(e) or "not enough rights" in str(e):
                    raise
                log("Прев'ю не вийшло, шлю картку й текст окремо:", e)
    if not long_text:
        data.update(caption=text, parse_mode="HTML")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", data=data,
                      files={"photo": ("card.jpg", card_bytes, "image/jpeg")}, timeout=120).json()
    new_id = (r.get("parameters") or {}).get("migrate_to_chat_id")
    if not r.get("ok") and new_id:
        # Групу перетворили на супергрупу (з гілками) — шлемо за новою адресою, у гілку «Тести»
        print(f"Чат {chat_id} став супергрупою {new_id}; оновіть змінну CHANNEL_ID")
        chat_id = new_id
        data["chat_id"] = new_id
        if str(new_id) == TEST_GROUP_ID:
            data["message_thread_id"] = TEST_THREAD
        r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", data=data,
                          files={"photo": ("card.jpg", card_bytes, "image/jpeg")}, timeout=120).json()
    if not r.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto: {r}")
    if long_text:
        extra = {"reply_markup": keyboard} if keyboard else {}
        if str(chat_id) == TEST_GROUP_ID:
            extra["message_thread_id"] = TEST_THREAD
        tg("sendMessage", _token=token, chat_id=chat_id, text=text, parse_mode="HTML",
           disable_notification=silent, link_preview_options={"is_disabled": True}, **extra)
    return r["result"]


DRAFT_KEYBOARD = {"inline_keyboard": [
    [{"text": "🔄 Інша людина", "callback_data": "replace"},
     {"text": "🖼 Без вирізання", "callback_data": "nocut"}],
    [{"text": "⛔ Не публікувати", "callback_data": "skip"},
     {"text": "✅ Опублікувати зараз", "callback_data": "now"}],
]}


PICK_KEYBOARD = {"inline_keyboard": [
    [{"text": "1️⃣ Варіант 1 (вирізана)", "callback_data": "pick1"},
     {"text": "2️⃣ Варіант 2 (овал)", "callback_data": "pick2"}],
] + DRAFT_KEYBOARD["inline_keyboard"]}


KEYBOARD_MSGS = []   # (чат, повідомлення) з кнопками за цей запуск — після публікації кнопки прибираємо


def reviewers():
    """Хто перевіряє чернетки: Юля (REVIEW_CHAT_ID) + новинарі (EXTRA_REVIEWERS через кому).
    Усі отримують однакові чернетки й можуть натискати кнопки (з 24.11)."""
    ids = [str(env("REVIEW_CHAT_ID"))]
    for x in (env("EXTRA_REVIEWERS") or "").replace(";", ",").split(","):
        x = x.strip()
        if x and x not in ids:
            ids.append(x)
    return ids


def review_send(text, card_bytes=None, keyboard=None, exclude=None):
    """Повідомлення всім, хто перевіряє чернетки (exclude — кому не слати)."""
    first = None
    for chat in reviewers():
        if exclude and chat == str(exclude):
            continue
        try:
            if card_bytes is not None:
                r = tg_send(chat, text, card_bytes, silent=False, token=review_token(), keyboard=keyboard)
            else:
                extra = {"reply_markup": keyboard} if keyboard else {}
                r = tg("sendMessage", _token=review_token(), chat_id=chat, text=text, **extra)
        except Exception as e:
            if chat == str(env("REVIEW_CHAT_ID")):
                raise                               # Юлі — обов'язково; іншим — не зупиняємо роботу
            log("Не вдалося надіслати", chat, ":", e)
            continue
        if keyboard and isinstance(r, dict) and r.get("message_id") and "inline_keyboard" in keyboard:
            KEYBOARD_MSGS.append((chat, r["message_id"]))
        first = first or r
    return first


def close_buttons(note):
    """Після публікації чи скасування: прибирає кнопки з чернеток і пише, що вже вирішено,
    щоб натискання після 9:00 не лишалися без відповіді (20.11)."""
    for chat, mid in KEYBOARD_MSGS:
        try:
            tg("editMessageReplyMarkup", _token=review_token(), chat_id=chat,
               message_id=mid, reply_markup={"inline_keyboard": []})
        except Exception as e:
            log("Не вдалося прибрати кнопки:", e)
    KEYBOARD_MSGS.clear()
    try:
        review_send(note, keyboard={"remove_keyboard": True})
    except Exception as e:
        log("Повідомлення про завершення:", e)


# ---------- Facebook ----------

def facebook_check():
    """Перевірка без публікації: до яких сторінок має доступ ключ і що на них дозволено (ключ не друкуємо)."""
    token = env("FACEBOOK_PAGE_TOKEN")
    if not token:
        log("FACEBOOK_PAGE_TOKEN не задано")
        return
    r = requests.get("https://graph.facebook.com/v21.0/me/accounts",
                     params={"fields": "id,name,tasks", "access_token": token}, timeout=60).json()
    if "error" in r:
        log("Facebook помилка:", r["error"].get("message"))
        return
    for p in r.get("data", []):
        log(f"Сторінка {p.get('name')} ({p.get('id')}): {', '.join(p.get('tasks', []))}")
    me = requests.get("https://graph.facebook.com/v21.0/me", params={"fields": "id,name", "access_token": token},
                      timeout=60).json()
    log("Ключ належить:", me.get("name"), me.get("id"))


def facebook_mention_test():
    """Прихований тест (published=false, читачі не бачать): чи стають відмітки сторінок «Меморіалу» клікабельними.
    Після перевірки тестове фото видаляється."""
    token, page = env("FACEBOOK_PAGE_TOKEN"), "414302258746592"      # жовта сторінка
    G = "https://graph.facebook.com/v21.0"
    ids = {}
    for name in ("victims.of.russia", "memorialofheroes"):
        r = requests.get(f"{G}/{name}", params={"fields": "id,name", "access_token": token}, timeout=60).json()
        log("Сторінка", name, "→", r.get("id"), r.get("name"), (r.get("error") or {}).get("message", ""))
        if r.get("id"):
            ids[name] = r["id"]
    pt = requests.get(f"{G}/{page}", params={"fields": "access_token", "access_token": token}, timeout=60).json()
    ptoken = pt.get("access_token") or token
    caption = "Тест відмітки (прихований, буде видалено). " + " ".join(
        f"@[{i}]" for i in ids.values()) + " | текстом: @victims.of.russia @memorialofheroes"
    img = Image.new("RGB", (400, 400), (40, 40, 40))
    buf = io.BytesIO(); img.save(buf, "JPEG")
    r = requests.post(f"{G}/{page}/photos", data={"caption": caption, "published": "false", "access_token": ptoken},
                      files={"source": ("t.jpg", buf.getvalue(), "image/jpeg")}, timeout=120).json()
    log("Публікація прихованого фото:", {k: v for k, v in r.items() if k != "access_token"})
    pid = r.get("id")
    if not pid:
        return
    info = requests.get(f"{G}/{pid}", params={"fields": "name,name_tags", "access_token": ptoken}, timeout=60).json()
    log("Текст після публікації:", info.get("name"))
    log("Розпізнані відмітки:", info.get("name_tags"), (info.get("error") or {}).get("message", ""))
    d = requests.delete(f"{G}/{pid}", params={"access_token": ptoken}, timeout=60).json()
    log("Тестове фото видалено:", d)


FB_PAGE = "414302258746592"        # жовта сторінка «Антикризовий медіа-центр»
FB_START = date(2026, 11, 28)      # перший день «Хвилини мовчання» на Facebook (рішення Юлі 24.11)
FB_API = "https://graph.facebook.com/v21.0"


def facebook_comment_text(person):
    return f"Історія на платформі пам'яті «Меморіал»: {person.url}"   # без імені — щоб не відмінювати


def facebook_publish(person, region, card_bytes=None):
    """Жовта сторінка: картка як фото + текст без посилань; посилання на історію — першим коментарем."""
    token = env("FACEBOOK_PAGE_TOKEN")
    if not token:
        log("Facebook: немає FACEBOOK_PAGE_TOKEN — пропускаю")
        return None
    pt = requests.get(f"{FB_API}/{FB_PAGE}", params={"fields": "access_token", "access_token": token},
                      timeout=60).json()
    ptoken = pt.get("access_token") or token
    text = build_post(person, region, html_mode=False, network="facebook")
    card_bytes = card_bytes or make_card(person, region)
    data = requests.post(f"{FB_API}/{FB_PAGE}/photos", data={"caption": text, "access_token": ptoken},
                         files={"source": ("card.jpg", card_bytes, "image/jpeg")}, timeout=180).json()
    if "error" in data:
        raise RuntimeError(f"Facebook: {data['error'].get('message')}")
    post_id = data.get("post_id") or data.get("id")
    time.sleep(5)
    c = requests.post(f"{FB_API}/{post_id}/comments",
                      data={"message": facebook_comment_text(person), "access_token": ptoken}, timeout=60).json()
    if "error" in c:
        log("Facebook: допис є, але коментар з посиланням не додався:", c["error"].get("message"))
        review_send(f"⚠️ Facebook: допис вийшов, але перший коментар не додався. "
                    f"Додай вручну: {facebook_comment_text(person)}")
    return data


def facebook_preview():
    """Показ у «Тестах», як виглядатиме допис для Facebook (сьогоднішня людина). На Facebook нічого не йде."""
    pending = load(PENDING_FILE, {})
    person = memorial.Person(**pending["person"])
    region = pending["region"]
    card_bytes = make_card(person, region, nocut=pending.get("nocut", False),
                           contour=pending.get("contour"), erase=pending.get("erase"))
    text = build_post(person, region, html_mode=False, network="facebook")
    tg_send(TEST_GROUP_ID, "📘 ТАК ВИГЛЯДАТИМЕ ДОПИС НА ЖОВТІЙ СТОРІНЦІ FACEBOOK (з 28.11)", card_bytes,
            token=review_token())
    for i in range(0, len(text), 4000):
        tg("sendMessage", _token=review_token(), chat_id=TEST_GROUP_ID, message_thread_id=TEST_THREAD,
           text=text[i:i + 4000], disable_web_page_preview=True, disable_notification=True)
    tg("sendMessage", _token=review_token(), chat_id=TEST_GROUP_ID, message_thread_id=TEST_THREAD,
       text="💬 Перший коментар під дописом:\n" + facebook_comment_text(person),
       disable_web_page_preview=True, disable_notification=True)


# ---------- сценарії ----------

POSTPONE_DAYS = 45


def postponed_ids(published, day: date):
    """Люди, яких редакторка замінила кнопкою «Інша людина» нещодавно — тимчасово не беремо."""
    out = []
    for pid, d in published.get("postponed", {}).items():
        try:
            if (day - date.fromisoformat(d)).days < POSTPONE_DAYS:
                out.append(pid)
        except ValueError:
            pass
    return out


def prepare(day: date, dry_run=False):
    index = load(INDEX_FILE, None)
    if not index or not index.get("people"):
        index = update_index(full=True)
    else:
        index = update_index(full=False)
    published = load(PUBLISHED_FILE, {"rejected": [], "log": []})
    rng = random.Random()
    channel = env("CHANNEL_ID")
    skip = list(published["rejected"]) + postponed_ids(published, day)
    person, region = choose(index, [p["id"] for p in published["log"] if p.get("channel") == channel],
                            skip, day, rng)
    if not dry_run:
        save(PUBLISHED_FILE, published)  # зберігаємо відхилених
    if not person:
        raise RuntimeError("Не знайдено жодної людини для поста")
    text = build_post(person, region)
    returning = person.id in published.get("postponed", {})
    review_send(text, make_card(person, region))
    if returning and not dry_run:
        # цю людину вже колись замінювали — показуємо ще й варіант без вирізання, щоб обрати
        review_send("Варіант 2 — те саме фото без вирізання (м'яким овалом):",
                    make_card(person, region, nocut=True))
        review_send("↩️ Цю людину ти вже колись замінювала, тож ось два варіанти картки. "
                    "Якщо нічого не натиснеш — о 9:00 вийде варіант 1.", keyboard=PICK_KEYBOARD)
    if dry_run:
        review_send("👆 Чернетка «Хвилини мовчання». Це тест, у канал не піде.")
    elif not returning:
        review_send(f"👆 Чернетка «Хвилини мовчання». Вийде о 9:00 {day.strftime('%d.%m')}.\n"
                    "Якщо треба — натисни кнопку нижче.", keyboard=DRAFT_KEYBOARD)
    if not dry_run:
        offer_contour(person)
    if not dry_run:
        save(PENDING_FILE, {"date": day.isoformat(), "person": person.to_dict(),
                            "region": region, "preview_ts": int(time.time()),
                            "published": False})
    return person, region


def edit(action):
    """Правки редакторки до сьогоднішньої чернетки (кнопки в Telegram або режими на GitHub).
    Повертає True, якщо на сьогодні все вирішено (опубліковано чи скасовано)."""
    today = datetime.now(KYIV).date().isoformat()
    pending = load(PENDING_FILE, {})
    if pending.get("date") != today or pending.get("published"):
        review_send("На сьогодні немає неопублікованої чернетки — змінювати нічого.")
        return True
    person = memorial.Person(**pending["person"])
    if action == "skip":
        pending["published"] = "cancelled"
        save(PENDING_FILE, pending)
        close_buttons("⛔ Добре, сьогодні «Хвилину мовчання» не публікуємо.")
        return True
    if action == "now":
        review_send("✅ Публікую зараз…")
        publish(pending)
        close_buttons("✅ Опубліковано. Кнопки під чернеткою вже не діють.")
        return True
    if action in ("pick1", "pick2"):
        pending["nocut"] = action == "pick2"
        save(PENDING_FILE, pending)
        review_send(f"👍 Обрано варіант {action[-1]}. Вийде о 9:00.", keyboard=DRAFT_KEYBOARD)
        return False
    if action == "nocut":
        pending["nocut"] = True
        pending.pop("contour", None)
        pending.pop("erase", None)
        save(PENDING_FILE, pending)
        review_send(build_post(person, pending["region"]),
                    make_card(person, pending["region"], nocut=True))
        review_send("👆 Нова картка — фото без вирізання. Вийде о 9:00.", keyboard=DRAFT_KEYBOARD)
    elif action == "replace":
        review_send("🔄 Шукаю іншу людину…")
        published = load(PUBLISHED_FILE, {"rejected": [], "log": []})
        # не «опрацьована» назавжди: людина повернеться в добірку через POSTPONE_DAYS днів
        published.setdefault("postponed", {})[person.id] = datetime.now(KYIV).date().isoformat()
        save(PUBLISHED_FILE, published)
        if not os.environ.get("FORCE_CATEGORY"):
            os.environ["FORCE_CATEGORY"] = person.cat                # та сама категорія
        prepare(datetime.now(KYIV).date())
    return False


def contour_received(data):
    """Редакторка обвела людину — перемальовуємо картку за її контуром."""
    pending = load(PENDING_FILE, {})
    person = memorial.Person(**pending["person"])
    if str(data.get("id")) != str(person.id):
        review_send("Цей контур — для попередньої чернетки. Обведи людину з поточної.")
        offer_contour(person)
        return
    if data.get("p"):                       # новий контур — попередні мазки гумки вже не потрібні
        pending["contour"] = data["p"]
        pending.pop("erase", None)
    if data.get("e") and data["e"].get("s"):  # гумка додається до попередніх мазків
        er = pending.get("erase") or {"r": data["e"].get("r", 18), "s": []}
        er["s"] = er["s"] + data["e"]["s"]
        er["r"] = data["e"].get("r", er["r"])
        pending["erase"] = er
    what = "контуром" if data.get("p") and not data.get("e") else "твоїми правками"
    review_send(f"✏️ Дякую! Перемальовую картку за {what}…", keyboard={"remove_keyboard": True})
    pending["nocut"] = False
    save(PENDING_FILE, pending)
    review_send(build_post(person, pending["region"]),
                make_card(person, pending["region"], contour=pending.get("contour"), erase=pending.get("erase")))
    review_send("👆 Нова картка. Вийде о 9:00, якщо нічого не зміниш. "
                "Лишилось зайве — 🧽 «Стерти зайве» ще раз.", keyboard=DRAFT_KEYBOARD)
    offer_contour(person)


ACTION_NAMES = {"replace": "🔄 «Інша людина»", "nocut": "🖼 «Без вирізання»", "skip": "⛔ «Не публікувати»",
                "now": "✅ «Опублікувати зараз»", "pick1": "варіант 1", "pick2": "варіант 2"}


def tell_others(who, what):
    """Іншим перевіряльникам: хто що натиснув — щоб не робили те саме вдвох."""
    name = (who.get("first_name") or "Хтось").strip()
    review_send(f"👤 {name} натискає {what}.", exclude=who.get("id"))


def listen(until: datetime):
    """Слухає кнопки під чернеткою до вказаного часу. True — сьогодні вже вирішено."""
    token = review_token()
    allowed = set(reviewers())
    try:   # пропускаємо старі натискання
        old = tg("getUpdates", _token=token, offset=-1, timeout=0)
        offset = old[-1]["update_id"] + 1 if old else None
    except Exception as e:
        log("Кнопки недоступні:", e)
        wait = (until - datetime.now(KYIV)).total_seconds()
        if wait > 0:
            time.sleep(wait)
        return False
    log("Слухаю кнопки до", until.strftime("%H:%M"))
    while True:
        left = int((until - datetime.now(KYIV)).total_seconds())
        if left <= 0:
            return False
        try:
            updates = tg("getUpdates", _token=token, offset=offset, timeout=min(50, left),
                         allowed_updates=["callback_query", "message"])
        except Exception as e:
            log("getUpdates:", e)
            time.sleep(10)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            if m.get("web_app_data") and str(m.get("from", {}).get("id")) in allowed:
                if len(allowed) > 1:
                    tell_others(m["from"], "✏️ «Обвести контур» / 🧽 «Стерти зайве»")
                try:
                    contour_received(json.loads(m["web_app_data"]["data"]))
                except Exception as e:
                    log("Контур:", e)
                    review_send(f"⚠️ Не вдалося застосувати контур: {e}")
                continue
            q = u.get("callback_query")
            if not q:
                continue
            try:
                tg("answerCallbackQuery", _token=token, callback_query_id=q["id"])
            except Exception:
                pass
            if str(q.get("from", {}).get("id")) not in allowed:
                continue
            action = q.get("data")
            log("Кнопка:", action, "від", q["from"].get("first_name"))
            if len(allowed) > 1:
                tell_others(q["from"], ACTION_NAMES.get(action, action))
            try:
                if edit(action):
                    return True
            except Exception as e:
                log("Помилка дії", action, e)
                review_send(f"⚠️ Не вдалося: {e}")


def publish(pending):
    person = memorial.Person(**pending["person"])
    region = pending["region"]
    to_channel = env("PUBLISH_TO_CHANNEL", "false").lower() == "true"
    target = env("CHANNEL_ID") if to_channel else env("REVIEW_CHAT_ID")
    card_bytes = make_card(person, region, nocut=pending.get("nocut", False),
                           contour=pending.get("contour"), erase=pending.get("erase"))
    # публікує «меморіальний» бот; якщо його ще не додали в чат — старий бот
    try:
        # зі звуком: о 9:00 сповіщення нагадує зупинитися на хвилину мовчання
        tg_send(target, build_post(person, region), card_bytes, silent=False, token=review_token())
    except RuntimeError as e:
        if env("MEMORIAL_BOT_TOKEN") and ("chat not found" in str(e) or "Forbidden" in str(e)
                                          or "not enough rights" in str(e)):
            log("Новий бот не має доступу до чату — публікує старий:", e)
            tg_send(target, build_post(person, region), card_bytes, silent=False)
        else:
            raise
    log("Telegram: опубліковано в", "канал" if to_channel else "чат перевірки (тестовий режим)")

    if to_channel and date.fromisoformat(pending["date"]) >= FB_START:
        try:
            if facebook_publish(person, region, card_bytes):
                log("Facebook: опубліковано на жовтій сторінці")
        except Exception as e:
            log("Facebook не вдалося:", e)
            review_send(f"⚠️ Facebook: допис не вийшов ({e}). У Telegram усе гаразд.")

    published = load(PUBLISHED_FILE, {"rejected": [], "log": []})
    if to_channel:
        published.get("postponed", {}).pop(person.id, None)      # пом'янули — більше не «відкладена»
        published["log"].append({"id": person.id, "name": person.name, "cat": person.cat,
                                 "region": region, "date": pending["date"], "url": person.url,
                                 "channel": env("CHANNEL_ID")})
    save(PUBLISHED_FILE, published)
    pending["published"] = True
    save(PENDING_FILE, pending)


def sync_state(push=False, msg="Оновлення стану"):
    """Синхронізує папку state з GitHub, щоб паралельні запуски бачили одне одного."""
    def run(*a):
        return subprocess.run(list(a), check=False, capture_output=True, text=True)
    if push:
        run("git", "add", "state")
        if run("git", "diff", "--cached", "--quiet").returncode:
            run("git", "-c", "user.name=memorial-bot", "-c",
                "user.email=memorial-bot@users.noreply.github.com", "commit", "-m", msg)
    run("git", "pull", "--quiet", "--rebase", "--autostash", "-X", "theirs")
    if push:
        r = run("git", "push")
        if r.returncode:
            log("Не вдалося зберегти стан:", r.stderr.strip()[:300])


def auto():
    sync_state()                                  # свіжий стан: можливо, інший запуск уже все зробив
    now = datetime.now(KYIV)
    today = now.date()
    pending = load(PENDING_FILE, {})
    if pending.get("date") == today.isoformat() and pending.get("published"):
        log("Сьогодні вже опубліковано або скасовано.")
        return
    if not (now.replace(hour=6, minute=0) <= now < now.replace(hour=10, minute=0)):
        log("Не ранкове вікно — нічого не роблю.")
        return
    nine = now.replace(hour=9, minute=0, second=0, microsecond=0)

    if pending.get("date") != today.isoformat():
        if now >= nine.replace(hour=8, minute=30):
            # запізнилися: без перевірки редакторки не публікуємо — лише чернетка з кнопками
            log("Запізно для звичайного графіка — чернетка без автопублікації")
            prepare(today)
            sync_state(push=True, msg="Чернетка (запізніла)")
            review_send("⏰ Чернетка підготувалася із запізненням, тому сама не вийде. "
                        "Якщо все добре — натисни «✅ Опублікувати зараз» протягом 40 хв.")
            listen(datetime.now(KYIV) + timedelta(minutes=40))
            return
        log("Готую пост на сьогодні")
        prepare(today)
        sync_state(push=True, msg=f"Чернетка {today}")
        pending = load(PENDING_FILE, {})

    if datetime.now(KYIV) < nine:
        log(f"До 9:00 {int((nine - datetime.now(KYIV)).total_seconds() // 60)} хв — слухаю кнопки")
        if listen(nine):
            return
    # редакторка могла змінити чернетку через GitHub — беремо свіжий стан
    sync_state()
    pending = load(PENDING_FILE, {})
    if pending.get("date") != today.isoformat() or pending.get("published"):
        log("Чернетку скасовано або вже опубліковано.")
        return
    publish(pending)
    sync_state(push=True, msg=f"Опубліковано {today}")
    close_buttons("✅ «Хвилина мовчання» вийшла о 9:00. Кнопки під чернеткою вже не діють.\n"
                  "Якщо в дописі треба щось виправити — напиши Claude, бот відредагує допис, зберігши картку.")


def link_preview_demo(pid="9934"):
    """Перевірка формату «картка-прев'ю + текст» на довгій історії. Лише редакторці."""
    entry = load(INDEX_FILE, {})["people"][pid]
    person = memorial.get_person(entry["url"], entry["cat"])
    region = entry["regions"][0]
    review_send("👇 Перевірка нового формату (так виглядатиме допис у каналі). Кнопки тут не діють.")
    review_send(build_post(person, region), make_card(person, region))


def fix_post(message_id):
    """Переписує вже опублікований у каналі сьогоднішній допис: свіжий текст (з виправленнями коду)
    і картка-прев'ю над текстом. Для випадків, коли картка зникла після ручного редагування (20.11)."""
    pending = load(PENDING_FILE, {})
    person = memorial.Person(**pending["person"])
    region = pending["region"]
    card_bytes = make_card(person, region, nocut=pending.get("nocut", False),
                           contour=pending.get("contour"), erase=pending.get("erase"))
    url = host_card(card_bytes)
    if not url:
        raise RuntimeError("Не вдалося викласти картку")
    text = f'<a href="{url}">\u2060</a>' + build_post(person, region)
    tg("editMessageText", _token=review_token(), chat_id=env("CHANNEL_ID"), message_id=int(message_id),
       text=text, parse_mode="HTML",
       link_preview_options={"url": url, "prefer_large_media": True, "show_above_text": True})
    log("Допис", message_id, "у каналі оновлено")


def who_started():
    """Хто писав боту (наприклад, натиснув «Старт»): номери чатів і імена. Нічого не надсилає
    і не позначає повідомлення прочитаними (getUpdates без offset)."""
    lines = []
    for u in tg("getUpdates", _token=review_token(), timeout=0, allowed_updates=["message"]):
        m = u.get("message") or {}
        f = m.get("from") or {}
        line = (f"{f.get('id')} | {f.get('first_name','')} {f.get('last_name','')} | @{f.get('username','')} | "
                f"{(m.get('text') or '')[:20]} | {datetime.fromtimestamp(m.get('date', 0), KYIV):%d.%m %H:%M}")
        log(line)
        lines.append(line)
    # Список — лише Юлі в особистий чат (з 25.11.2026), щоб не шукати номер у журналі GitHub
    text = ("👤 Хто писав Меморіал-боту (ID | ім'я | @нік | текст | час):\n" + "\n".join(lines)) if lines \
        else "👤 Нових повідомлень боту немає. Попросіть колегу ще раз натиснути «Старт» і запустіть перевірку знову."
    tg("sendMessage", _token=review_token(), chat_id=env("REVIEW_CHAT_ID"), text=text[:4000])


def check_channel():
    """Перевіряє, чи може Меморіал-бот публікувати в CHANNEL_ID; звіт — редакторці."""
    ch = env("CHANNEL_ID")
    tok = review_token()
    me = tg("getMe", _token=tok)
    try:
        info = tg("getChat", _token=tok, chat_id=ch)
        m = tg("getChatMember", _token=tok, chat_id=ch, user_id=me["id"])
        ok = m.get("status") == "administrator" and m.get("can_post_messages")
        msg = (f"Канал «{info.get('title')}» ({ch}): бот @{me.get('username')} — {m.get('status')}, "
               f"публікувати: {'так' if m.get('can_post_messages') else 'ні'}, "
               f"видаляти: {'так' if m.get('can_delete_messages') else 'ні'}.")
        review_send(("✅ " if ok else "⚠️ ") + msg)
    except RuntimeError as e:
        review_send(f"⚠️ Не вдалося перевірити канал {ch}: {e}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if mode == "index":
        update_index(full="--full" in sys.argv)
    elif mode == "birthscan":                # щонеділі о 20:00 — хто з загиблих народився в містах регіону
        sync_state()
        update_index(weekly=True)
        sync_state(push=True, msg="Тижнева перевірка міст народження")
    elif mode == "test":
        os.environ["EXTRA_REVIEWERS"] = ""        # тестова чернетка — лише Юлі, новинарів не турбуємо
        prepare(datetime.now(KYIV).date(), dry_run=True)
    elif mode == "now":
        # тест: обрати людину й одразу опублікувати в CHANNEL_ID, без очікування 9:00
        prepare(datetime.now(KYIV).date())
        pending = load(PENDING_FILE, {})
        pending["preview_ts"] = int(time.time())
        publish(pending)
    elif mode == "auto":
        auto()
    elif mode == "buttons":
        # тест кнопок: свіжа чернетка з кнопками, слухаємо 45 хвилин
        prepare(datetime.now(KYIV).date())
        if not listen(datetime.now(KYIV) + timedelta(minutes=45)):
            review_send("⏱ Тест кнопок завершено (45 хв минуло) — кнопки більше не діють.",
                        keyboard={"remove_keyboard": True})
    elif mode in ("replace", "nocut", "skip"):
        edit(mode)
    elif mode == "fixpost":
        fix_post(env("MESSAGE_ID"))
    elif mode == "fbpreview":
        facebook_preview()
    elif mode == "whostarted":
        who_started()
    elif mode == "fbcheck":
        facebook_check()
    elif mode == "fbmention":
        facebook_mention_test()
    elif mode == "checkchannel":
        check_channel()
    elif mode == "linkpreview":
        link_preview_demo()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
