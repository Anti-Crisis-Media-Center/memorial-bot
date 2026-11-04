"""Робота з сайтом memorial.ua: завантаження списків і сторінок людей, розбір HTML."""
import re
import time
from dataclasses import dataclass, field, asdict

import requests
from bs4 import BeautifulSoup

from regions import normalize

BASE = "https://memorial.ua"
CATEGORIES = ("civilians", "militaries", "children")
HEADERS = {"User-Agent": "ACMC-memorial-minute-bot/1.0 (+https://acmc.ua)"}
DELAY = 0.7  # пауза між запитами, щоб не навантажувати сайт «Меморіалу»

MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}

_session = requests.Session()
_session.headers.update(HEADERS)


def fetch(path: str) -> str:
    url = path if path.startswith("http") else BASE + path
    for attempt in range(3):
        try:
            r = _session.get(url, timeout=30)
            r.raise_for_status()
            time.sleep(DELAY)
            return r.text
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    return ""


def person_id(url: str) -> str:
    m = re.search(r"-(\d+)/?$", url)
    return m.group(1) if m else url


def parse_date(text: str):
    """'09 квітня 2024' -> (2024, 4, 9) або None"""
    m = re.search(r"(\d{1,2})\s+([а-яіїє]+)\s+(\d{4})", normalize(text or ""))
    if not m or m.group(2) not in MONTHS:
        return None
    return int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))


# ---------- списки ----------

def parse_list(html: str, category: str):
    """Повертає (картки, номер останньої сторінки)."""
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for item in soup.select("div.person-short"):
        a = item.select_one("a.media")
        if not a or not a.get("href", "").startswith(f"/obituaries/{category}/"):
            continue
        url = a["href"]
        first = item.select_one(".firs-name")
        last = item.select_one(".last-name")
        photo = item.select_one("img.photo")
        fields = {}
        for f in item.select(".person-field-value"):
            t = f.select_one(".title")
            v = f.select_one(".value")
            if t and v:
                fields[normalize(t.get_text())] = normalize(v.get_text()).rstrip(", ")
        desc = item.select_one(".desc")
        cards.append({
            "id": person_id(url),
            "cat": category,
            "url": url,
            "name": normalize(f"{first.get_text() if first else ''} {last.get_text() if last else ''}"),
            "photo": photo.get("src") if photo else None,
            "fields": fields,
            "desc": normalize(desc.get_text(" ")) if desc else "",
        })
    last_page = 1
    for link in soup.select(".m-pagination a, a[href*='page=']"):
        m = re.search(r"page=(\d+)", link.get("href", ""))
        if m:
            last_page = max(last_page, int(m.group(1)))
    return cards, last_page


def list_url(category: str, page: int, death_oblast_code=None, birth_city_id=None) -> str:
    url = f"/obituaries/{category}?page={page}"
    if death_oblast_code:
        url += f"&filter=o50%3D{death_oblast_code}"
    if birth_city_id:
        url += f"&filter=l48%3D{birth_city_id}"
    return url


def birth_city_options(html: str) -> dict:
    """Назва міста -> [id] з випадного списку «Місто народження» (l48)."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    sel = soup.select_one("select#l48")
    if sel:
        for o in sel.find_all("option"):
            name = normalize(o.get_text())
            if o.get("value") and name:
                out.setdefault(name, []).append(o["value"])
    return out


# ---------- сторінка людини ----------

@dataclass
class Person:
    id: str
    cat: str
    url: str
    name: str
    photo: str = None
    desc: str = ""
    occupation: str = ""
    fields: dict = field(default_factory=dict)   # назва поля -> [значення]
    story: list = field(default_factory=list)    # абзаци «Історії життя»

    def f(self, name: str) -> str:
        return ", ".join(self.fields.get(name, []))

    def to_dict(self):
        return asdict(self)


def parse_person(html: str, url: str, category: str) -> Person:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.select_one("h1.name") or soup.select_one("h1")
    heading = soup.select_one(".heading")
    photo = heading.select_one("img.photo") if heading else None
    desc = heading.select_one(".desc") if heading else None
    occ = heading.select_one(".occupation") if heading else None

    fields = {}
    for item in soup.select(".fields .list .item, #fieldsList .item"):
        t = item.select_one(".title .name") or item.select_one(".title")
        if not t:
            continue
        vals = [normalize(v.get_text()) for v in item.select(".value")]
        vals = [v for v in vals if v and v != "-"]
        if vals:
            fields[normalize(t.get_text())] = vals

    story = []
    container = soup.select_one("#personStory .article-text")
    if container:
        for p in container.find_all(["p", "h3", "h4", "li"]):
            text = normalize(p.get_text(" "))
            text = re.sub(r"\s+([,.;:!?])", r"\1", text)
            # «***» відділяє історію від службової приписки Меморіалу (партнери тощо) — далі не беремо
            cut = re.split(r"\*\s*\*\s*\*", text, maxsplit=1)
            if cut[0].strip():
                story.append(cut[0].strip())
            if len(cut) > 1:
                break

    return Person(
        id=person_id(url),
        cat=category,
        url=BASE + url if url.startswith("/") else url,
        name=normalize(h1.get_text()) if h1 else "",
        photo=photo.get("src") if photo else None,
        desc=normalize(desc.get_text(" ")) if desc else "",
        occupation=normalize(occ.get_text()) if occ else "",
        fields=fields,
        story=story,
    )


def get_person(url: str, category: str) -> Person:
    return parse_person(fetch(url), url, category)
