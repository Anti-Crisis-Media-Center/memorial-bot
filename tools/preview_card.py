"""Прев'ю карток на конкретних людях із бази (для налаштування дизайну).
Запуск: python tools/preview_card.py ID [ID ...] → preview.jpg"""
import io, json, signal, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests
from PIL import Image
import bot, card, memorial
index = json.load(open("state/index.json"))
people = index.get("people", index)
import os
tiles = []
log = []
def timed(name, fn):
    def w(*a, **k):
        t = time.time(); r = fn(*a, **k); log.append(f"  {name}: {time.time() - t:.1f} s")
        open("timings.txt", "w").write("\n".join(log)); return r
    return w
card.upscale = timed("upscale", card.upscale)
card.cut_person = timed("cut_person", card.cut_person)
jobs = []
for arg in sys.argv[1:]:                      # ID або ID@файл_фону (варіант синього для цивільних)
    arg, _, cfile = arg.partition("%")       # ID%файл_контуру.json — прев'ю з контуром редакторки
    pid, _, bg = arg.partition("@")
    if cfile:
        bg = bg + "%" + cfile
    jobs.append((pid, bg))
for pid, bg in jobs:
    bg, _, cfile = bg.partition("%")
    contour = json.load(open(cfile)) if cfile else None
    erase = None
    if isinstance(contour, dict):              # {"p": контур, "e": гумка}
        contour, erase = contour.get("p"), contour.get("e")
    if bg:
        os.environ["CIVIL_BG"] = bg
    else:
        os.environ.pop("CIVIL_BG", None)
    t0 = time.time()
    log.append(f"{pid} start")
    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(TimeoutError()))
    signal.alarm(300)
    p = memorial.parse_person(memorial.fetch(people[pid]["url"]), people[pid]["url"], people[pid]["cat"])
    region = "luhansk" if "Луган" in json.dumps(people[pid], ensure_ascii=False) else "donetsk"
    try:
        tiles.append(Image.open(io.BytesIO(bot.make_card(p, region, contour=contour, erase=erase))).resize((720, 720)))
        log.append(f"{pid}: {time.time() - t0:.0f} s")
    except TimeoutError:
        log.append(f"{pid}: TIMEOUT")
    signal.alarm(0)
    open("timings.txt", "w").write("\n".join(log))
out = Image.new("RGB", (740 * len(tiles), 720), (245, 245, 245))
for i, t in enumerate(tiles):
    out.paste(t, (i * 740, 0))
out.save("cutout_test.jpg", quality=88)
