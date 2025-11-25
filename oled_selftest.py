from time import sleep
from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from PIL import Image, ImageDraw, ImageFont

ADDR = 0x3C
dev = ssd1306(i2c(port=1, address=ADDR), width=128, height=64)
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 18)
except Exception:
    font = ImageFont.load_default()

text = " OLED OK | Hello 128x64 | "
img = Image.new("1", (1, 16), 0); d = ImageDraw.Draw(img)
w = int(d.textlength(text, font=font)) + 10
strip = Image.new("1", (w, 16), 0); d = ImageDraw.Draw(strip)
d.text((0,0), text, fill=1, font=font)

x = 0
while True:
    canvas = Image.new("1", (dev.width, dev.height), 0)
    seg = strip.crop((x, 0, min(x+dev.width, w), 16))
    canvas.paste(seg, (0, 8))
    if x+dev.width > w:
        seg2 = strip.crop((0, 0, dev.width-(w-x), 16))
        canvas.paste(seg2, (w-x, 8))
    dev.display(canvas)
    x = (x+2) % w
    sleep(1/20)
