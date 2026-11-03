"""Порівняння моделей вирізання фону на одному фото (для налаштування)."""
import io, sys, time, requests
from PIL import Image
from rembg import remove, new_session
im = Image.open(io.BytesIO(requests.get(sys.argv[1], timeout=60).content)).convert("RGB")
outs = []
for m in sys.argv[2:]:
    t = time.time()
    r = remove(im, session=new_session(m))
    bg = Image.new("RGB", im.size, (255, 0, 255)); bg.paste(r, (0, 0), r); outs.append(bg)
    print(m, round(time.time() - t, 1), "s")
o = Image.new("RGB", (im.width * len(outs), im.height))
for i, x in enumerate(outs): o.paste(x, (im.width * i, 0))
o.save("cutout_test.jpg")
