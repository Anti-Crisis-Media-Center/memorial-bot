"""Прев'ю карток на конкретних людях із бази (для налаштування дизайну).
Запуск: python tools/preview_card.py ID [ID ...] → preview.jpg"""
import io, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests
from PIL import Image
import bot, card, memorial
index = json.load(open("state/index.json"))
people = index.get("people", index)
import os
tiles = []
jobs = []
for arg in sys.argv[1:]:                      # ID або ID@файл_фону (варіант синього для цивільних)
    pid, _, bg = arg.partition("@")
    jobs.append((pid, bg))
for pid, bg in jobs:
    if bg:
        os.environ["CIVIL_BG"] = bg
    else:
        os.environ.pop("CIVIL_BG", None)
    p = memorial.parse_person(memorial.fetch(people[pid]["url"]), people[pid]["url"], people[pid]["cat"])
    region = "luhansk" if "Луган" in json.dumps(people[pid], ensure_ascii=False) else "donetsk"
    tiles.append(Image.open(io.BytesIO(bot.make_card(p, region))).resize((720, 720)))
out = Image.new("RGB", (740 * len(tiles), 720), (245, 245, 245))
for i, t in enumerate(tiles):
    out.paste(t, (i * 740, 0))
out.save("cutout_test.jpg", quality=88)
