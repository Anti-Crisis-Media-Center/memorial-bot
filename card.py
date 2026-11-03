"""Картка для соцмереж у стилі АКМЦ.

«Меморіал» просить не публікувати в соцмережах сирі фото з сайту, бо родичам
це може бути боляче, а лише оформлені картки. Тож бот сам робить картку:
фото людини, ім'я, професія чи підрозділ, місто, роки життя, свічка й логотип.
"""
import io
from pathlib import Path

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).parent
FONTS = ROOT / "fonts"
LOGO = ROOT / "logo.png"          # логотип АКМЦ (білі лінії на прозорому тлі)
MARKS = {                         # знаки «Меморіалу»: голуб — цивільні, жетон — військові
    "civilians": ROOT / "marks" / "dove.png",
    "children": ROOT / "marks" / "dove.png",
    "militaries": ROOT / "marks" / "tag.png",
}

W, H = 1080, 1080                 # квадрат — добре для Facebook, Instagram і Telegram
BG_TOP = (35, 51, 88)             # #233358 — темно-синій АКМЦ
BG_BOTTOM = (16, 22, 38)
WHITE = (255, 255, 255)
MUTED = (190, 198, 214)
YELLOW = (255, 204, 51)           # #FFCC33 — жовтий АКМЦ
LIGHT_BLUE = (58, 94, 178)        # #3A5EB2 — світло-синій АКМЦ
ACCENT = {                        # колір рамки й позначки міста за категорією
    "civilians": YELLOW,
    "children": YELLOW,
    "militaries": YELLOW,          # рамка однакова для всіх; військових вирізняє жетон
}
SUBTITLE = {                      # професія / підрозділ — без акценту, світлий текст
    "civilians": (226, 230, 238),
    "children": (226, 230, 238),
    "militaries": (226, 230, 238),
}


def font(weight, size):
    return ImageFont.truetype(str(FONTS / f"Montserrat-{weight}.ttf"), size)


def wrap(draw, text, fnt, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=fnt) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def gradient(size, top, bottom):
    g = Image.new("RGB", (1, size[1]))
    for y in range(size[1]):
        t = y / (size[1] - 1)
        g.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return g.resize(size)


def fade_mask(size, left=0.35, top=0.25, right=0.10, bottom=0.18):
    """Маска, щоб фото плавно розчинялося в тлі зліва і зверху."""
    w, h = size
    mask = Image.new("L", size, 255)
    px = mask.load()
    lw, th = int(w * left), int(h * top)
    for y in range(h):
        for x in range(w):
            a = 255
            if x < lw:
                a = min(a, int(255 * (x / lw) ** 1.4))
            if y < th:
                a = min(a, int(255 * (y / th) ** 1.4))
            bh = int(h * bottom)
            if bh and y > h - bh:
                a = min(a, int(255 * ((h - y) / bh) ** 1.4))
            rw = int(w * right)
            if rw and x > w - rw:
                a = min(a, int(255 * ((w - x) / rw) ** 1.4))
            px[x, y] = a
    return mask


def draw_pin(d, x, y, color, s=1.0):
    r = int(13 * s)
    d.ellipse((x - r, y - r, x + r, y + r), fill=color)
    d.polygon([(x - r * 0.85, y + r * 0.45), (x + r * 0.85, y + r * 0.45), (x, y + r * 2.3)], fill=color)
    d.ellipse((x - r * 0.4, y - r * 0.4, x + r * 0.4, y + r * 0.4), fill=BG_TOP)


def draw_candle(d, x, y, color):
    """Проста свічка: полум'я + тіло."""
    d.rounded_rectangle((x - 9, y + 18, x + 9, y + 62), 4, fill=WHITE)
    d.line((x, y + 12, x, y + 19), fill=MUTED, width=2)
    d.ellipse((x - 8, y - 10, x + 8, y + 14), fill=color)
    d.polygon([(x - 7, y - 1), (x + 7, y - 1), (x, y - 22)], fill=color)


_SESSION = None


def cut_person(photo: Image.Image):
    """Вирізає людину з фото (прибирає тло). Якщо бібліотеки немає — повертає None."""
    global _SESSION
    try:
        from rembg import new_session, remove
    except ImportError:
        return None
    if _SESSION is None:
        _SESSION = new_session("u2net_human_seg")
    return remove(photo.convert("RGB"), session=_SESSION)


def halo(size, center, radius, color, strength):
    """М'яке світле коло за людиною, щоб вирізане фото не виглядало пласким."""
    layer = Image.new("RGBA", size, color + (0,))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse((center[0] - radius, center[1] - radius,
                                   center[0] + radius, center[1] + radius), fill=strength)
    layer.putalpha(mask.filter(ImageFilter.GaussianBlur(radius * 0.45)))
    return layer


def make_card(photo: Image.Image, *, category, name, subtitle, city, years, credit=True) -> bytes:
    accent = ACCENT.get(category, ACCENT["civilians"])
    card = gradient((W, H), BG_TOP, BG_BOTTOM)

    # --- фото: чорно-біле, праворуч унизу, з м'яким переходом у тло
    cut = cut_person(photo)
    # дуотон: темні ділянки фото стають темно-синіми
    gray = ImageOps.autocontrast(ImageOps.grayscale(photo), cutoff=1)
    ph = ImageOps.colorize(gray, black=BG_BOTTOM, white=(250, 250, 250), mid=(118, 126, 146))
    target_h = int(H * 0.66)
    scale = target_h / ph.height
    size = (int(ph.width * scale), target_h)
    ph = ph.resize(size, Image.LANCZOS).filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))
    px = W - 44 - ph.width          # праве ребро фото всередині рамки
    py = H - 31 - ph.height - 90    # піднято, низ плавно зникає
    if cut is not None:
        # тло прибрано: людина на тлі картки + м'яке світло за головою
        card = Image.alpha_composite(card.convert("RGBA"),
                                     halo((W, H), (px + size[0] // 2, py + size[1] // 3),
                                          int(size[0] * 0.55), (90, 110, 160), 120)).convert("RGB")
        # м'який контур: трохи «з'їдаємо» край і розмиваємо, щоб людина переходила в тло, а не була наклеєна
        alpha = cut.getchannel("A").resize(size, Image.LANCZOS)
        alpha = alpha.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.GaussianBlur(10))
        mask = ImageChops.multiply(alpha, fade_mask(size, left=0, top=0, right=0, bottom=0.22))
    else:
        mask = fade_mask(size)
    card.paste(ph, (px, py), mask)

    # затемнення внизу, щоб підпис читався поверх фото
    shade_h = 300
    shade = Image.new("RGBA", (W, shade_h), BG_BOTTOM + (0,))
    sa = Image.linear_gradient("L").resize((W, shade_h)).point(lambda v: int(v * 0.9))
    shade.putalpha(sa)
    card.paste(shade, (0, H - shade_h), shade)

    d = ImageDraw.Draw(card)
    margin = 72
    col_w = 470                    # ліва колонка тексту — не заходить на обличчя

    # --- тонка рамка
    d.rectangle((28, 28, W - 29, H - 29), outline=accent, width=3)

    # --- ім'я: кожне слово з нового рядка; шрифт зменшується, якщо слово довге
    y = 96
    size = 84
    while size > 52 and max(d.textlength(p, font=font("Bold", size)) for p in name.split()) > col_w + 60:
        size -= 4
    name_font = font("Bold", size)
    for part in name.split():
        d.text((margin, y), part, font=name_font, fill=WHITE)
        y += int(size * 1.13)
    y += 14

    # --- роки життя одразу під ім'ям
    if years:
        d.text((margin, y), years, font=font("SemiBold", 36), fill=YELLOW)
        y += 62

    # --- професія / підрозділ
    if subtitle:
        sub_font = font("Medium", 34)
        for line in wrap(d, subtitle, sub_font, col_w)[:3]:
            d.text((margin, y), line, font=sub_font, fill=SUBTITLE.get(category, YELLOW))
            y += 44
        y += 20

    # --- місто
    if city:
        draw_pin(d, margin + 13, y + 14, YELLOW)
        city_font = font("SemiBold", 34)
        for line in wrap(d, city, city_font, col_w - 44)[:2]:
            d.text((margin + 42, y), line, font=city_font, fill=WHITE)
            y += 44

    # --- низ: знак «Меморіалу» + підпис
    mark_path = MARKS.get(category)
    mh = 150 if category == "militaries" else 104   # жетон з ниткою — трохи більший
    base = H - 80 - 104 - (mh - 104) // 2
    tx = margin
    if mark_path and mark_path.exists():
        mark = Image.open(mark_path).convert("RGBA")
        mark = mark.resize((int(mark.width * mh / mark.height), mh), Image.LANCZOS)
        card.paste(mark, (margin - (10 if category == "militaries" else 0), base), mark)
        tx = margin + mark.width + 26
    d.text((tx, base + (mh - 104) // 2 + 16), "Хвилина мовчання", font=font("Bold", 34), fill=YELLOW)
    if credit:
        d.text((tx, base + (mh - 104) // 2 + 60), "Платформа пам'яті «Меморіал»", font=font("Medium", 24), fill=MUTED)
    if LOGO.exists():
        # логотип тонколінійний, тож на малому розмірі трохи потовщуємо лінії
        logo = Image.open(LOGO).convert("RGBA")
        lw = 170
        logo = logo.resize((lw, int(logo.height * lw / logo.width)), Image.LANCZOS)
        alpha = logo.getchannel("A").filter(ImageFilter.MaxFilter(3))
        logo.putalpha(alpha)
        card.paste(logo, (W - lw - 64, 64), logo)   # правий верхній кут — там темне тло

    out = io.BytesIO()
    card.save(out, "JPEG", quality=92)
    return out.getvalue()


def card_for_person(person, city: str) -> bytes:
    from memorial import parse_date
    raw = requests.get(person.photo, timeout=60).content
    photo = Image.open(io.BytesIO(raw))
    if person.cat == "militaries":
        subtitle = ", ".join(person.fields.get("Військовий підрозділ", [])[:1]) or person.occupation
    else:
        subtitle = person.occupation
    b = parse_date(person.f("Дата народження"))
    dd = parse_date(person.f("Дата загибелі"))
    years = f"{b[0]} — {dd[0]}" if b and dd else ""
    return make_card(photo, category=person.cat, name=person.name, subtitle=subtitle,
                     city=city, years=years)
