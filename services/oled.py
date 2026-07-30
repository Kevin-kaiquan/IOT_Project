# -*- coding: utf-8 -*-
"""OLED carousel display for CO2, temperature, light, and humidity."""

import time
from threading import Thread, Event, Lock
from collections import deque
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from luma.core.interface.serial import i2c
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
try:
    from luma.oled.device import ssd1306 as OLED_DEVICE
except Exception:
    from luma.oled.device import sh1106 as OLED_DEVICE

I2C_BUS = 1
I2C_ADDR = 0x3C
ROTATE = 0
FPS = 20
PAGE_HOLD_SECS = 0.5
REBUILD_EVERY = 0.6
BIG_FONT_SIZE = 26
MID_FONT_SIZE = 16
SMALL_FONT_SIZE = 13


class OledDisplay:
    """Paginated carousel for CO2, temperatures, light, and humidity."""

    def __init__(self, bus=I2C_BUS, addr=I2C_ADDR, rotate=ROTATE, fps=FPS):
        serial = i2c(port=bus, address=addr)
        self.dev = OLED_DEVICE(serial, rotate=rotate)
        self.W, self.H = self.dev.width, self.dev.height

        self.font_big = self._try_font(BIG_FONT_SIZE)
        self.font_mid = self._try_font(MID_FONT_SIZE)
        self.font_small = self._try_font(SMALL_FONT_SIZE)

        self._q = deque(maxlen=1)
        self._lock = Lock()
        self._evt = Event()

        self._running = True
        self._min_dt = 1.0 / max(1, int(fps))

        self._page = 0
        self._last_page_ts = 0.0
        self._last_rebuild_ts = 0.0
        self._page_img: Optional[Image.Image] = None

        self._flash_text: Optional[str] = None
        self._flash_until: float = 0.0

        self.dev.clear()
        self._t = Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self):
        self._running = False
        self._evt.set()
        if self._t.is_alive():
            self._t.join(timeout=1.0)

    def flash(self, text: str, sec: float = 2.0):
        """Temporarily overlay text for a short duration."""
        self._flash_text = str(text)
        self._flash_until = time.time() + max(0.2, float(sec))
        self._evt.set()

    def show_numbers(self, co2_ppm=None, t1=None, t2=None, rh=None, light=None):
        """Feed the latest sensor values for display."""
        with self._lock:
            self._q.append({
                "co2": None if co2_ppm is None else float(co2_ppm),
                "t1": None if t1 is None else float(t1),
                "t2": None if t2 is None else float(t2),
                "rh": None if rh is None else float(rh),
                "light": None if light is None else float(light),
            })
        self._evt.set()

    def _try_font(self, size: int) -> ImageFont.ImageFont:
        try:
            return ImageFont.truetype(FONT_PATH, size)
        except Exception:
            return ImageFont.load_default()

    def _fmt(self, v, d=1, unit=""):
        return "--" if v is None else f"{v:.{d}f}{unit}"

    def _center_text(self, d: ImageDraw.ImageDraw, y: int, text: str, font):
        w = int(d.textlength(text, font=font))
        x = (self.W - w) // 2
        d.text((x, y), text, font=font, fill=255)

    def _build_page_img(self, page: int, payload: dict) -> Image.Image:
        """Render a single display page based on the carousel index."""
        img = Image.new("1", (self.W, self.H), 0)
        d = ImageDraw.Draw(img)

        if page == 0:
            title = "CO2"
            self._center_text(d, 0, title, self.font_small)
            val = f"{self._fmt(payload.get('co2'), 0)} ppm"
            self._center_text(d, 20, val, self.font_big)

        elif page == 1:
            self._center_text(d, 0, "Probe Temps", self.font_small)
            t1 = f"T1: {self._fmt(payload.get('t1'), 2, '°C')}"
            t2 = f"T2: {self._fmt(payload.get('t2'), 2, '°C')}"
            self._center_text(d, 22, t1, self.font_mid)
            self._center_text(d, 42, t2, self.font_mid)

        elif page == 2:
            self._center_text(d, 0, "Light", self.font_small)
            val = f"{self._fmt(payload.get('light'), 1, 'lux')}"
            self._center_text(d, 20, val, self.font_big)
        else:
            self._center_text(d, 0, "Humidity", self.font_small)
            val = f"{self._fmt(payload.get('rh'), 1, '%')}"
            self._center_text(d, 20, val, self.font_big)

        return img

    def _latest_payload(self) -> dict:
        with self._lock:
            return self._q[-1] if self._q else {"co2": None, "t1": None, "t2": None, "rh": None, "light": None}

    def _loop(self):
        last = 0.0
        while self._running:
            self._evt.wait(timeout=self._min_dt)
            self._evt.clear()
            now = time.time()
            if now - last < self._min_dt:
                continue
            last = now

            if self._flash_text and now < self._flash_until:
                try:
                    self._render_flash(self._flash_text)
                except Exception:
                    pass
                continue
            else:
                self._flash_text = None

            if (now - self._last_page_ts) >= float(PAGE_HOLD_SECS):
                self._page = (self._page + 1) % 4
                self._last_page_ts = now
                self._page_img = None

            need_rebuild = (self._page_img is None) or ((now - self._last_rebuild_ts) > float(REBUILD_EVERY))
            if need_rebuild:
                payload = self._latest_payload()
                try:
                    self._page_img = self._build_page_img(self._page, payload)
                    self._last_rebuild_ts = now
                except Exception:
                    pass

            if self._page_img is not None:
                try:
                    self.dev.display(self._page_img)
                except Exception:
                    pass

    def _render_flash(self, text: str):
        img = Image.new("1", (self.W, self.H), 0)
        d = ImageDraw.Draw(img)
        lines = str(text).split("\n")[:2]
        y0 = (self.H - 2 * 16) // 2
        for i, ln in enumerate(lines):
            d.text((4, y0 + i * 16), ln, font=self.font_mid, fill=255)
        self.dev.display(img)
