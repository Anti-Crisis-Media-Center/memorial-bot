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
BG_TOP = (60, 60, 60)             # #3c3c3c — графіт з брендбука АКМЦ
BG_BOTTOM = (28, 27, 26)
WHITE = (255, 255, 255)
MUTED = (190, 198, 214)
PHOTO_DARK = (28, 27, 26)         # тіні фото — як низ тла
PHOTO_MID = (132, 128, 122)       # теплі півтони фото
PHOTO_LIGHT = (250, 247, 240)
YELLOW = (255, 204, 51)           # #FFCC33 — жовтий АКМЦ
NAME_COLOR = YELLOW               # ім'я — жовтим
YEARS_COLOR = (255, 255, 255)     # роки життя — білим
LIGHT_BLUE = (58, 94, 178)        # #3A5EB2 — світло-синій АКМЦ
ACCENT = {                        # колір рамки: білий для всіх
    "civilians": (235, 235, 235),
    "children": (235, 235, 235),
    "militaries": (235, 235, 235),
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

# Скільки місця займає людина: висота обличчя = 20% висоти картки,
# центр обличчя — праворуч (70% ширини), трохи нижче середини (47% висоти).
FACE_H = 0.20
FACE_CX, FACE_CY = 0.66, 0.47


def find_face(photo: Image.Image):
    """Найнадійніше обличчя на фото (детектор YuNet, OpenCV): (x, y, w, h) або None."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    model = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
    if not model.exists():
        return None
    img = cv2.cvtColor(np.asarray(photo.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = img.shape[:2]
    det = cv2.FaceDetectorYN.create(str(model), "", (w, h), score_threshold=0.7)
    _, faces = det.detect(img)
    if faces is None or len(faces) == 0:
        return None
    best = max(faces, key=lambda f: f[14] * f[2] * f[3])     # впевненість × розмір
    x, y, fw, fh = (int(v) for v in best[:4])
    return x, y, fw, fh


ESRGAN_URL = "https://huggingface.co/SceneWorks/real-esrgan-onnx/resolve/main/real_esrgan_x2.onnx"
_UPSCALER = None


def _models_dir() -> Path:
    import os
    return Path(os.environ.get("U2NET_HOME", Path.home() / ".u2net"))


def upscale(photo: Image.Image) -> Image.Image:
    """Збільшує маленьке фото вдвічі нейромережею Real-ESRGAN (з домальовуванням деталей).
    Фото з Меморіалу зазвичай 400×500 — після збільшення контур і обличчя чіткіші.
    Якщо модель недоступна — повертає фото як є."""
    global _UPSCALER
    if max(photo.size) >= 1000:
        return photo
    try:
        import numpy as np
        import onnxruntime as ort
        if _UPSCALER is None:
            path = _models_dir() / "real_esrgan_x2.onnx"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                with requests.get(ESRGAN_URL, stream=True, timeout=120) as r:
                    r.raise_for_status()
                    with open(path.with_suffix(".part"), "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
                path.with_suffix(".part").rename(path)
            _UPSCALER = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        x = np.asarray(photo.convert("RGB"), dtype="float32").transpose(2, 0, 1)[None] / 255.0
        y = _UPSCALER.run(None, {_UPSCALER.get_inputs()[0].name: x})[0][0]
        y = (y.clip(0, 1) * 255).round().astype("uint8").transpose(1, 2, 0)
        return Image.fromarray(y)
    except Exception as e:                                   # без збільшення картка все одно буде
        print("upscale: пропущено —", e)
        return photo


def cut_person(photo: Image.Image):
    """Вирізає людину з фото (прибирає тло).

    Контур веде birefnet-portrait з обробкою країв (alpha matting) — так акуратніше волосся.
    Друга модель, u2net_human_seg, «бачить» лише людину: великі шматки, які birefnet
    вважає людиною, а вона — ні (рюкзак, чужа рука), вирізаються. Дрібні розбіжності
    по краю не чіпаємо, щоб контур лишався рівним.
    Якщо моделі сильно не згодні між собою (темний одяг на темному тлі тощо) — повертає
    None, і картка бере фото м'яким овалом без вирізання."""
    global _SESSION
    try:
        import cv2
        import numpy as np
        from rembg import new_session, remove
    except ImportError:
        return None
    if _SESSION is None:
        _SESSION = [new_session("birefnet-portrait"), new_session("u2net_human_seg")]
    rgb = photo.convert("RGB")
    try:
        out = remove(rgb, session=_SESSION[0], alpha_matting=True,
                     alpha_matting_foreground_threshold=235,
                     alpha_matting_background_threshold=15, alpha_matting_erode_size=8)
    except Exception:                                        # matting інколи падає — тоді без нього
        out = remove(rgb, session=_SESSION[0])
    a = np.array(out.getchannel("A")).astype("float32")
    b = np.array(remove(rgb, session=_SESSION[1]).getchannel("A")).astype("float32")
    # великі «чужі» шматки: є в birefnet, немає в human_seg
    foreign = ((a > 128) & (b < 40)).astype("uint8")
    k = max(5, int(min(a.shape) * 0.035)) | 1
    foreign = cv2.morphologyEx(foreign, cv2.MORPH_OPEN, np.ones((k, k), "uint8"))
    foreign = cv2.dilate(foreign, np.ones((7, 7), "uint8"))
    a = a * (1 - foreign)
    # наскільки моделі згодні щодо силуету
    am, bm = a > 128, b > 128
    union = (am | bm).sum()
    iou = (am & bm).sum() / union if union else 0
    if iou < 0.80:
        print(f"cut_person: моделі не згодні (IoU {iou:.2f}) — фото без вирізання")
        return None
    out.putalpha(Image.fromarray(a.clip(0, 255).astype("uint8")))
    return out


def halo(size, center, radius, color, strength):
    """М'яке світле коло за людиною, щоб вирізане фото не виглядало пласким."""
    layer = Image.new("RGBA", size, color + (0,))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse((center[0] - radius, center[1] - radius,
                                   center[0] + radius, center[1] + radius), fill=strength)
    layer.putalpha(mask.filter(ImageFilter.GaussianBlur(radius * 0.45)))
    return layer


def keep_main_figure(alpha: Image.Image) -> Image.Image:
    """Залишає в масці лише найбільшу суцільну фігуру (людину), прибирає залишки тла."""
    import cv2
    import numpy as np
    a = np.array(alpha)
    n, labels, stats, _ = cv2.connectedComponentsWithStats((a > 100).astype("uint8"), 8)
    if n <= 2:
        return alpha
    main = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    keep = cv2.dilate((labels == main).astype("uint8"), np.ones((7, 7), "uint8"))
    return Image.fromarray((a * keep).astype("uint8"))


BACKGROUNDS = {                   # підкладка від дизайнера: рваний папір кольору категорії
    "civilians": ROOT / "backgrounds" / "civilians_birds.jpg",  # графіт із птахами + теплий графітово-синій папір
    "children": ROOT / "backgrounds" / "civilians_birds.jpg",
    "militaries": ROOT / "backgrounds" / "militaries.jpg",
}


def _bg_path(category):
    import os
    alt = os.environ.get("CIVIL_BG")
    if alt and category in ("civilians", "children"):
        return ROOT / "backgrounds" / alt
    return BACKGROUNDS.get(category)


def background(category: str):
    """Підкладка картки: файл дизайнера, обрізаний до квадрата (низ із папером зберігається)."""
    path = _bg_path(category)
    if not path or not Path(path).exists():
        return gradient((W, H), BG_TOP, BG_BOTTOM)
    bg = Image.open(path).convert("RGB")
    bg = bg.resize((W, round(bg.height * W / bg.width)), Image.LANCZOS)
    bg = bg.crop((0, bg.height - H, W, bg.height))
    if Path(path).stem.endswith("_birds"):        # тло з птахами вже готове: темний графіт, світлі птахи
        return bg
    # темна частина підкладки майже чорна — піднімаємо її до графіту АКМЦ (#3c3c3c),
    # зберігаючи ледь помітну текстуру; папір і кольорова смуга не змінюються
    dark = ImageOps.grayscale(bg).point(lambda v: 255 if v < 40 else max(0, 255 - (v - 40) * 12))
    lifted = ImageChops.add(gradient((W, H), BG_TOP, BG_BOTTOM), bg.point(lambda v: int(v * 0.9)))
    return Image.composite(lifted, bg, dark.filter(ImageFilter.GaussianBlur(1)))


def make_card(photo: Image.Image, *, category, name, subtitle, city, years, credit=True) -> bytes:
    accent = ACCENT.get(category, ACCENT["civilians"])
    card = background(category)

    # --- фото: чорно-біле, праворуч унизу, з м'яким переходом у тло
    # Фото: людина праворуч, тло фото прибране, краї плавно розчиняються.
    # Масштаб — за обличчям, щоб на всіх картках воно було однакового розміру.
    photo = upscale(photo)
    gray = ImageOps.autocontrast(ImageOps.grayscale(photo), cutoff=1)
    duo = ImageOps.colorize(gray, black=PHOTO_DARK, white=PHOTO_LIGHT, mid=PHOTO_MID)
    face = find_face(photo)
    if face is not None:
        fx, fy, fw, fh = face
        fcy = fy + fh / 2
        scale = FACE_H * H / fh
        # якщо фото по плечі — збільшуємо, щоб низ фото діставав до низу картки (не «висіло» в повітрі),
        # але обличчя не більше 32% висоти картки
        scale = max(scale, (1 - FACE_CY) * H / max(1, photo.height - fcy))
        paper = bool(BACKGROUNDS.get(category))
        scale = min(scale, (0.38 if paper else 0.32) * H / fh, 2.4 * H / photo.height)
        ox = int(FACE_CX * W - (fx + fw / 2) * scale)
        oy = int(FACE_CY * H - (fy + fh / 2) * scale)
        # фото по плечі, яке не дістає до низу: опускаємо його (обличчя не нижче 60% висоти),
        # щоб людина не «висіла» в повітрі
        bottom = oy + photo.height * scale
        target = H if paper else H - 31        # на папері людина стоїть на самому низу картки
        if bottom < target:
            max_shift = int((0.74 if paper else 0.60) * H - (oy + fcy * scale))
            oy += max(0, min(int(target - bottom), max_shift))
    else:                                                  # обличчя не знайдено — як раніше
        scale = 0.80 * H / photo.height
        ox = W - int(photo.width * scale * 0.90)
        oy = H - int(photo.height * scale)
    size = (max(1, int(photo.width * scale)), max(1, int(photo.height * scale)))
    duo = duo.resize(size, Image.LANCZOS).filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))

    cut = cut_person(photo)
    on_paper = cut is not None and BACKGROUNDS.get(category)
    if on_paper:
        # на підкладці з папером людина вирізана чітко по контуру, без розчинення країв;
        # плавно зникає лише низ, якщо фото не дістає до низу картки
        alpha = cut.getchannel("A").resize(size, Image.LANCZOS)
        # контур уже акуратний (matting); прибираємо ледь помітний серпанок і м'яко розмиваємо край на 1–2 px
        alpha = alpha.point(lambda v: 0 if v < 18 else min(255, int((v - 18) * 255 / 190)))
        alpha = keep_main_figure(alpha)
        alpha = alpha.filter(ImageFilter.GaussianBlur(1.3))
        # людина не заходить на колонку тексту: зсуваємо праворуч (обличчя лишається в кадрі)
        bbox = alpha.point(lambda v: 255 if v > 40 else 0).getbbox()
        if bbox and ox + bbox[0] < 545:
            face_x = ox + (fx + fw / 2) * scale if face is not None else ox + size[0] / 2
            ox += int(max(0, min(545 - (ox + bbox[0]), W - 260 - face_x)))
        # голова (з вухами) не виходить за білу рамку праворуч
        if face is not None:
            head_right = ox + (fx + fw * 1.25) * scale
            if head_right > W - 60:
                ox -= int(head_right - (W - 60))
        # тіло обрізане правим краєм фото → ставимо цей зріз упритик до правого краю картки
        # (якщо при цьому голова з вухами лишається в рамці)
        a_np = alpha.crop((size[0] - 3, 0, size[0], size[1])).getextrema()
        if a_np[1] > 100:
            ox_edge = W - size[0]
            if face is not None:                # вухо не далі ніж на 40 px від краю картки
                ox_edge = min(ox_edge, int(W - 40 - (fx + fw * 1.2) * scale))
            ox = max(ox, ox_edge)
        right_in = ox + size[0] < W - 2
        # спершу чіткий контур, а розчиняється лише вузька смужка (~35 px) біля краю фото
        edge = 35 / size[0]
        alpha = ImageChops.multiply(alpha, fade_mask(size, left=edge, top=0, right=edge if right_in else 0, bottom=0))
    elif cut is not None:
        alpha = cut.getchannel("A").resize(size, Image.LANCZOS)
        alpha = alpha.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.GaussianBlur(8))
        alpha = ImageChops.multiply(alpha, fade_mask(size, left=0, top=0, right=0.06, bottom=0.18))
    elif BACKGROUNDS.get(category):
        # вирізати надійно не вийшло — фото м'яким овалом навколо обличчя, без різких меж
        alpha = Image.new("L", size, 0)
        cx = (fx + fw / 2) * scale if face is not None else size[0] / 2
        cy = (fy + fh / 2) * scale if face is not None else size[1] * 0.4
        rx, ry = size[0] * 0.48, size[1] * 0.75
        ImageDraw.Draw(alpha).ellipse((cx - rx, cy - ry * 0.6, cx + rx, cy + ry * 1.6), fill=255)
        alpha = alpha.filter(ImageFilter.GaussianBlur(size[0] * 0.08))
    else:
        alpha = fade_mask(size, left=0.35, top=0.25, right=0.06, bottom=0.18)

    layer = Image.new("RGB", (W, H))
    mask = Image.new("L", (W, H), 0)
    layer.paste(duo, (ox, oy))
    mask.paste(alpha, (ox, oy))
    # ліва колонка з текстом лишається чистою: людина м'яко зникає ліворуч
    ramp = Image.linear_gradient("L").rotate(90, expand=True).resize((W, H))   # 0 зліва → 255 справа
    ramp = ramp.point(lambda v: 0 if v < 100 else min(255, int((v - 100) * 255 / 70)))
    if BACKGROUNDS.get(category):
        ramp = Image.new("L", (W, H), 255)      # людина ціла, текст лягає поверх
    card.paste(layer, (0, 0), ImageChops.multiply(mask, ramp))

    # затемнення внизу, щоб підпис читався поверх фото
    shade_h = 300
    shade = Image.new("RGBA", (W, shade_h), BG_BOTTOM + (0,))
    sa = Image.linear_gradient("L").resize((W, shade_h)).point(lambda v: int(v * (0.55 if BACKGROUNDS.get(category) else 0.9)))
    shade.putalpha(sa)
    card.paste(shade, (0, H - shade_h), shade)

    if BACKGROUNDS.get(category):
        # темна підкладка під підписом «Хвилина мовчання» зліва внизу — для контрасту
        fh_, fw_ = 300, 760
        vy = Image.linear_gradient("L").resize((fw_, fh_))                       # 0 вгорі → 255 внизу
        hx = Image.linear_gradient("L").rotate(90, expand=True).resize((fw_, fh_))
        hx = ImageOps.invert(hx)                                                 # 255 зліва → 0 справа
        fm = ImageChops.multiply(vy.point(lambda v: min(255, int(v * 1.6))), hx.point(lambda v: min(255, int(v * 1.5))))
        card.paste((10, 10, 12), (0, H - fh_), fm.point(lambda v: int(v * 0.85)))

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
        d.text((margin, y), part, font=name_font, fill=NAME_COLOR)
        y += int(size * 1.13)
    y += 14

    # --- роки життя одразу під ім'ям
    if years:
        d.text((margin, y), years, font=font("SemiBold", 36), fill=YEARS_COLOR)
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
    lines = [((tx, base + (mh - 104) // 2 + 16), "Хвилина мовчання", font("Bold", 34), YELLOW)]
    if credit:
        lines.append(((tx, base + (mh - 104) // 2 + 60), "Платформа пам'яті «Меморіал»",
                      font("SemiBold", 24), (232, 234, 240)))
    # м'яка тінь під підписом — читається і на папері, і на одязі людини
    sh = Image.new("L", (W, H), 0)
    sd = ImageDraw.Draw(sh)
    for xy, text, f, _ in lines:
        sd.text((xy[0] + 2, xy[1] + 3), text, font=f, fill=230)
    card.paste((0, 0, 0), (0, 0), sh.filter(ImageFilter.GaussianBlur(5)))
    for xy, text, f, color in lines:
        d.text(xy, text, font=f, fill=color)
    if LOGO.exists():
        # лінії логотипа вже потовщені у файлі logo.png, тут лише якісне зменшення
        logo = Image.open(LOGO).convert("RGBA")
        lw = 140
        logo = logo.resize((lw, int(logo.height * lw / logo.width)), Image.LANCZOS)
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
