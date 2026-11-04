"""Порівняння моделей вирізання фону на одному фото (для налаштування).
Останній кадр — перетин масок усіх моделей."""
import io, sys, time, requests
from PIL import Image, ImageChops
from rembg import remove, new_session
im = Image.open(io.BytesIO(requests.get(sys.argv[1], timeout=60).content)).convert("RGB")
outs, masks = [], []
def show(mask):
    bg = Image.new("RGB", im.size, (255, 0, 255)); bg.paste(im, (0, 0), mask); return bg
for m in sys.argv[2:]:
    t = time.time()
    r = remove(im, session=new_session(m)); masks.append(r.getchannel("A")); outs.append(show(masks[-1]))
    print(m, round(time.time() - t, 1), "s")
inter = masks[0]
for m in masks[1:]:
    inter = ImageChops.darker(inter, m)
outs.append(show(inter))
o = Image.new("RGB", (im.width * len(outs), im.height))
for i, x in enumerate(outs): o.paste(x, (im.width * i, 0))
o.save("cutout_test.jpg")
