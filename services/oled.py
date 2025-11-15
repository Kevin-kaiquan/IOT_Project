# services/oled.py
# -*- coding: utf-8 -*-
"""
OLED 分页轮播显示（非滚动）：
  Page1: CO2 大字（ppm）
  Page2: T1/T2 两行
  Page3: RH 大字（%）
- 线程刷新；只使用“最新”数据，不积压
- 可调每页停留时间（默认 1.0s），确保跟得上网页 3s 刷新
- 兼容 flash(text, sec) 临时覆盖提示
"""

import time
from threading import Thread, Event, Lock
from collections import deque
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from luma.core.interface.serial import i2c
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
# 自动兼容 SSD1306 / SH1106（你的 0.96" 常见为 SSD1306）
try:
    from luma.oled.device import ssd1306 as OLED_DEVICE
except Exception:
    from luma.oled.device import sh1106 as OLED_DEVICE

# ======= 可按需调参（也可放到 config.py） =======
I2C_BUS          = 1
I2C_ADDR         = 0x3C         # i2cdetect -y 1 看是 3c/3d 决定
ROTATE           = 0            # 需要倒置就设 2
FPS              = 20           # 刷新上限（15~25 流畅）
PAGE_HOLD_SECS   = 0.5          # 每页停留时间（建议 0.8~1.2）
REBUILD_EVERY    = 0.6          # 同页内内容复渲染间隔（秒），避免频繁重绘
BIG_FONT_SIZE    = 26           # 大字
MID_FONT_SIZE    = 16           # 中字
SMALL_FONT_SIZE  = 13           # 小字
# ===============================================


class OledDisplay:
    """分页轮播：CO2 / T1T2 / RH"""

    def __init__(self, bus=I2C_BUS, addr=I2C_ADDR, rotate=ROTATE, fps=FPS):
        serial = i2c(port=bus, address=addr)
        self.dev = OLED_DEVICE(serial, rotate=rotate)
        self.W, self.H = self.dev.width, self.dev.height

        # 字体
        self.font_big  = self._try_font(BIG_FONT_SIZE)
        self.font_mid  = self._try_font(MID_FONT_SIZE)
        self.font_small= self._try_font(SMALL_FONT_SIZE)

        # 最新数据缓冲（只保留 1 份）
        self._q = deque(maxlen=1)
        self._lock = Lock()
        self._evt = Event()

        # 线程/节奏
        self._running = True
        self._min_dt = 1.0 / max(1, int(fps))

        # 轮播状态
        self._page = 0                   # 0,1,2
        self._last_page_ts = 0.0
        self._last_rebuild_ts = 0.0
        self._page_img: Optional[Image.Image] = None

        # flash 覆盖
        self._flash_text: Optional[str] = None
        self._flash_until: float = 0.0

        # 清屏并启动线程
        self.dev.clear()
        self._t = Thread(target=self._loop, daemon=True)
        self._t.start()

    # ---------- 对外接口 ----------
    def stop(self):
        self._running = False
        self._evt.set()

    def flash(self, text: str, sec: float = 2.0):
        """临时覆盖显示几秒（例如测试/报警提示）"""
        self._flash_text = str(text)
        self._flash_until = time.time() + max(0.2, float(sec))
        self._evt.set()

    def show_numbers(self, co2_ppm=None, t1=None, t2=None, rh=None,light=None):
        """喂入最新数值；任意为 None 时页面显示 --"""
        with self._lock:
            self._q.append({
                "co2": None if co2_ppm is None else float(co2_ppm),
                "t1":  None if t1      is None else float(t1),
                "t2":  None if t2      is None else float(t2),
                "rh":  None if rh      is None else float(rh),
                "light": None if light is None else float(light),
            })
        self._evt.set()

    # ---------- 内部实现 ----------
    def _try_font(self, size: int) -> ImageFont.ImageFont:
        try:
            return ImageFont.truetype(FONT_PATH, size)  # ← 用 TTF，支持 °
        except Exception:
            return ImageFont.load_default()  # 退回默认位图（可能不含 °）

    def _fmt(self, v, d=1, unit=""):
        return "--" if v is None else f"{v:.{d}f}{unit}"

    def _center_text(self, d: ImageDraw.ImageDraw, y: int, text: str, font):
        w = int(d.textlength(text, font=font))
        x = (self.W - w) // 2
        d.text((x, y), text, font=font, fill=255)

    def _build_page_img(self, page: int, payload: dict) -> Image.Image:
        """
        page=0: CO2
        page=1: T1/T2
        page=2: RH
        """
        img = Image.new("1", (self.W, self.H), 0)
        d = ImageDraw.Draw(img)

        if page == 0:
            # CO2 大字
            title = "CO2"
            self._center_text(d, 0, title, self.font_small)
            val = f"{self._fmt(payload.get('co2'), 0)} ppm"
            self._center_text(d, 20, val, self.font_big)

        elif page == 1:
            # T1/T2 两行
            self._center_text(d, 0, "Probe Temps", self.font_small)
            t1 = f"T1: {self._fmt(payload.get('t1'), 2, '°C')}"
            t2 = f"T2: {self._fmt(payload.get('t2'), 2, '°C')}"
            self._center_text(d, 22, t1, self.font_mid)
            self._center_text(d, 42, t2, self.font_mid)


        elif page == 2:

            # Light 光度数据（VELM7700）

            self._center_text(d, 0, "Light", self.font_small)

            val = f"{self._fmt(payload.get('light'), 1, 'lux')}"

            self._center_text(d, 20, val, self.font_big)
        else:
            # RH 大字
            self._center_text(d, 0, "Humidity", self.font_small)
            val = f"{self._fmt(payload.get('rh'), 1, '%')}"
            self._center_text(d, 20, val, self.font_big)

        return img

    def _latest_payload(self) -> dict:
        with self._lock:
            return self._q[-1] if self._q else {"co2":None, "t1":None, "t2":None, "rh":None}

    def _loop(self):
        last = 0.0
        while self._running:
            # 帧间隔
            self._evt.wait(timeout=self._min_dt)
            self._evt.clear()
            now = time.time()
            if now - last < self._min_dt:
                continue
            last = now

            # flash 优先
            if self._flash_text and now < self._flash_until:
                try:
                    self._render_flash(self._flash_text)
                except Exception:
                    pass
                continue
            else:
                self._flash_text = None

            # 是否切页
            if (now - self._last_page_ts) >= float(PAGE_HOLD_SECS):
                self._page = (self._page + 1) % 3
                self._last_page_ts = now
                self._page_img = None   # 切页后强制重建

            # 是否需要重建当前页（切页或超时或收到新数据）
            need_rebuild = (self._page_img is None) or ((now - self._last_rebuild_ts) > float(REBUILD_EVERY))
            if need_rebuild:
                payload = self._latest_payload()
                try:
                    self._page_img = self._build_page_img(self._page, payload)
                    self._last_rebuild_ts = now
                except Exception:
                    # 保持上一次的 img
                    pass

            # 显示当前页
            if self._page_img is not None:
                try:
                    self.dev.display(self._page_img)
                except Exception:
                    pass

    def _render_flash(self, text: str):
        img = Image.new("1", (self.W, self.H), 0)
        d = ImageDraw.Draw(img)
        lines = str(text).split("\n")[:2]
        y0 = (self.H - 2*16) // 2
        for i, ln in enumerate(lines):
            d.text((4, y0 + i*16), ln, font=self.font_mid, fill=255)
        self.dev.display(img)
