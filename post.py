"""Формування тексту поста за шаблоном АКМЦ."""
import html
import re

from memorial import parse_date
from regions import REGION_HASHTAGS, REGION_NAMES, normalize, region_from_field

OPENING = "🕯 Щодня о 9:00 Україна хвилиною мовчання вшановує загиблих у війні росії проти України."
CLOSING = "Вшануймо пам'ять хвилиною мовчання."

MONTHS_GEN = ["", "січня", "лютого", "березня", "квітня", "травня", "червня",
              "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]

# посилання на сторінки «Меморіалу» в соцмережах (за їхніми умовами — у тій самій мережі)
MEMORIAL_TELEGRAM = "https://t.me/memorial_ukraine"
MEMORIAL_FACEBOOK = {
    "militaries": "https://www.facebook.com/memorialofheroes/",
    "civilians": "https://www.facebook.com/victims.of.russia/",
    "children": "https://www.facebook.com/victims.of.russia/",
}


def age_words(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} рік"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} роки"
    return f"{n} років"


MALE_NAMES_A = ("Микола", "Нікола", "Микита", "Ілля", "Кузьма", "Сава", "Хома", "Лука", "Фома", "Ярема", "Данила",
                "Гаврила", "Савва")
FEMALE_NAMES_CONS = ("Любов", "Нінель", "Руфь", "Юдіф", "Есфір", "Агнес", "Ассоль")


def is_female(person) -> bool:
    """Стать — насамперед за ім'ям і заняттям загиблої людини.
    Текст історії лише як запасний варіант: у ньому часто говорять родичі
    («невістка розповідає, що у її свекра…») — і жіночі слова стосуються не загиблого (20.11, Віктор Ярко)."""
    words = person.name.split() if person.name else []
    first = words[0] if words else ""
    if len(words) > 1 and re.search(r"(енко|ук|юк|ський|цький|ич)$", first):
        first = words[1]                 # на сайті трапляється «Прізвище Ім'я»
    if first in MALE_NAMES_A:
        return False
    if first in FEMALE_NAMES_CONS:
        return True
    if first:
        return first.endswith(("а", "я"))
    occ = (getattr(person, "occupation", "") or "").lower()
    if re.search(r"(ка|иця|ня)\b", occ):
        return True
    text = person.desc.lower()           # опис — про саму людину, без переказів родичів
    fem = len(re.findall(r"\b(загинула|померла|народилася|народилась|була|працювала|мешкала|жила)\b", text))
    mas = len(re.findall(r"\b(загинув|помер|народився|був|працював|мешкав|жив)\b", text))
    return fem > mas


def human_date(text: str) -> str:
    d = parse_date(text)
    if not d:
        return text
    y, m, day = d
    return f"{day} {MONTHS_GEN[m]} {y} року"


def short_city(value: str) -> str:
    """"м. Слов'янськ, Донецька область" -> "Слов'янськ"; села лишають "с."."""
    city = normalize(value).split(",")[0].strip()
    return re.sub(r"^м\.\s*", "", city)


def city_line_city(person, region) -> str:
    born = person.f("Місто народження")
    if born and region_from_field(born) == region:
        return short_city(born)
    died = person.f("Місто загибелі")
    if person.cat != "militaries" and died and region_from_field(died) == region:
        return short_city(died)
    # місто відоме лише з тексту в іншому відмінку — пишемо назву області
    return REGION_NAMES[region]


def hashtag(word: str) -> str:
    word = re.sub(r"^(с\.|смт|с-ще)\s*", "", word)
    return "#" + re.sub(r"[^\wІіЇїЄєҐґ]", "", word)


PARTNER_NAME = "Антикризовим медіа-центром"   # «спільно з …» — орудний відмінок


def partner_line(person, html_mode, network):
    """Друге речення у форматі, який попросив «Меморіал»."""
    if person.cat == "militaries":
        whom = "полеглих захисниць" if is_female(person) else "полеглих захисників"
    else:
        whom = "загиблих цивільних"
    if html_mode and network == "telegram":
        name = f'<a href="{MEMORIAL_TELEGRAM}">Меморіал</a>'
    else:
        name = "Меморіал"
    return f"Платформа пам'яті {name} спільно з {PARTNER_NAME} вшановує пам'ять {whom}."


TG_LIMIT = 4000   # Telegram приймає до 4096 видимих символів у повідомленні; лишаємо запас


def _visible_len(text):
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def _trim_story(story, limit):
    """Скорочує історію до limit символів по межі речення, додає «…»."""
    out, used = [], 0
    for p in story:
        if used + len(p) <= limit:
            out.append(p)
            used += len(p) + 2
            continue
        room = limit - used
        sentences = re.split(r"(?<=[.!?…])\s+", p)
        part = ""
        for snt in sentences:
            if len(part) + len(snt) + 1 > room:
                break
            part = (part + " " + snt).strip()
        if part:
            out.append(part + " …")
        elif out:
            out[-1] = out[-1] + " …"
        break
    return out


def build_post(person, region, html_mode=True, network="telegram"):
    """Повертає текст поста. html_mode=True — розмітка для Telegram, False — простий текст (Facebook).
    Задовгу для Telegram історію скорочує по реченню (повна — за посиланням «читати на сайті»)."""
    text = _build_post(person, region, html_mode, network)
    if network == "telegram" and _visible_len(text) > TG_LIMIT:
        full = person.story
        over = _visible_len(text) - TG_LIMIT
        budget = max(300, sum(len(p) + 2 for p in full) - over - 20)
        while True:
            person.story = _trim_story(full, budget)
            text = _build_post(person, region, html_mode, network)
            if _visible_len(text) <= TG_LIMIT or budget <= 300:
                break
            budget -= 200
        person.story = full
        text = text.replace("читати на сайті</a>", "читати повністю на сайті</a>")
    return text


def _build_post(person, region, html_mode=True, network="telegram"):
    esc = (lambda s: html.escape(s, quote=False)) if html_mode else (lambda s: s)
    female = is_female(person)
    died_word = "загинула" if female else "загинув"

    age = person.f("Вік")
    age_n = int(age) if age.isdigit() else None
    death = person.f("Дата загибелі")
    city = city_line_city(person, region)

    # 1. Ім'я та вік
    if person.cat == "children" or age_n is None:
        title = person.name
    else:
        title = f"{person.name}, {age_words(age_n)}"
    lines = [OPENING + " " + partner_line(person, html_mode, network), ""]
    lines.append(f"<b>{esc(title)}</b>" if html_mode else title)

    # 2. Місто і дата загибелі — кожне окремим рядком: «Місто · загинув …» на телефоні
    # не вміщувався, і «року» падало на наступний рядок (пропозиція Тетяни Андрєєвої, 28.11)
    if city:
        lines.append(esc(city))
    if death:
        third = f"{died_word.capitalize()} {human_date(death)}"
        if person.cat == "children" and age_n is not None:
            third += f". {'Їй' if female else 'Йому'} було {age_words(age_n)}."
        lines.append(esc(third))
    elif person.cat == "children" and age_n is not None:
        lines.append(esc(f"{'Їй' if female else 'Йому'} було {age_words(age_n)}."))

    # 3. Посада і підрозділ для військових
    if person.cat == "militaries":
        role = person.fields.get("Військова посада", [])[:1]
        unit = person.fields.get("Військовий підрозділ", [])[:1]
        extra = ", ".join(role + unit)
        if extra:
            lines.append(esc(extra[0].upper() + extra[1:]))

    # 4. Історія
    lines.append("")
    for p in person.story:
        lines.append(esc(p))
        lines.append("")

    # 5. Завершення
    lines.append(CLOSING)
    if network == "telegram":
        src = MEMORIAL_TELEGRAM
    else:
        src = MEMORIAL_FACEBOOK.get(person.cat, MEMORIAL_FACEBOOK["civilians"])
    if network == "facebook":
        # Facebook (жовта сторінка, з 28.11): без посилань, хештегів і відміток у тексті —
        # Facebook ріже охоплення; посилання на історію — першим коментарем (рішення Юлі 23.11).
        # Джерело вказуємо обов'язково (п. 4.2 меморандуму).
        lines.append("Історію зберегла платформа пам'яті «Меморіал». Посилання на неї – у першому коментарі.")
        return "\n".join(lines).strip()
    if html_mode:
        lines.append(f'Історію зберегла платформа пам\'яті <a href="{src}">«Меморіал»</a> · '
                     f'<a href="{person.url}">читати на сайті</a>')
    else:
        lines.append(f"Історію зберегла платформа пам'яті «Меморіал»: {src}")
        lines.append(f"Читати на сайті: {person.url}")

    tags = ["#ХвилинаМовчання", REGION_HASHTAGS[region]]
    if city not in REGION_NAMES.values():
        tags.append(hashtag(city))
    lines.append("")
    lines.append(" ".join(tags))
    return "\n".join(lines).strip()
