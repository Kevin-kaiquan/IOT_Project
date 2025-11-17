#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主应用 app.py

修改点（按你的最新需求）：
1. 采集部分仍由 services.sampler.Sampler 完成，不改。
2. 环境控制逻辑：
   - 加热器（继电器）由 **两个温度探针 temp1 / temp2** 决定是否开启；
   - 风扇由 **CO₂ 浓度** 决定是否开启；
   - LED 由 **VEML7700 的光照值** 决定是否开启；
   - SHT4x 的湿度 rh_air 决定是否拉高 / 拉低 **树莓派 GPIO17**（也就是 ATOMIZER_PIN），
     通过 actuators.atomizer.Atomizer 来控制。

网页 /api/data 和 /api/atomizer 的接口保持不变，这样你的前端 dashboard 可以继续使用。
"""
import os
import sys
import time
import atexit
import logging
from typing import Optional

from flask import Flask, jsonify, render_template, request

try:
    import RPi.GPIO as GPIO  # type: ignore
except Exception:
    class _MockGPIO:
        BCM=BOARD=OUT=IN=LOW=HIGH=0
        def setwarnings(self,*a,**k): pass
        def setmode(self,*a,**k): pass
        def setup(self,*a,**k): pass
        def output(self,*a,**k): pass
        def cleanup(self,*a,**k): pass
    GPIO = _MockGPIO()  # type: ignore

from config import *

# ---- 保证项目根可导入 ----
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ---- 读取（或提供）配置 ----
try:
    import config as CFG
except Exception:
    class _CFG: ...
    CFG = _CFG()

SAMPLE_INTERVAL_SEC = getattr(CFG, "SAMPLE_INTERVAL_SEC", 3)
WX_OVERRIDE_SECONDS = getattr(CFG, "WX_OVERRIDE_SECONDS", 300)
WX_MAX_OVERRIDE_SECONDS = getattr(CFG, "WX_MAX_OVERRIDE_SECONDS", 3600)

# 环境控制用的 setpoint / 阈值（如未在 config.py 里定义，则用默认值）
TEMP_SETPOINT      = getattr(CFG, "TEMP_SETPOINT", 22.0)   # 加热目标温度（°C）
TEMP_TOLERANCE     = getattr(CFG, "TEMP_TOLERANCE", 0.5)   # 温度允许波动
CO2_HIGH_THRESHOLD = getattr(CFG, "CO2_HIGH_THRESHOLD", 1000.0)  # CO₂ 打开风扇阈值
CO2_LOW_THRESHOLD  = getattr(CFG, "CO2_LOW_THRESHOLD", 800.0)    # CO₂ 关闭风扇阈值
LIGHT_LOW_THRESHOLD = getattr(CFG, "LIGHT_LOW_THRESHOLD", 50.0)  # lux，低于此值就点亮 LED

# SHT4x 控制 GPIO17（雾化器 / 喷雾器）用的湿度阈值
HUMID_LOW_THRESHOLD  = getattr(CFG, "HUMID_LOW_THRESHOLD", 55.0)
HUMID_HIGH_THRESHOLD = getattr(CFG, "HUMID_HIGH_THRESHOLD", 65.0)

# 目标生长环境（用于前端差异提示）
IDEAL_ENVIRONMENT = {
    "temp_c": TEMP_SETPOINT,
    "humidity": (HUMID_LOW_THRESHOLD + HUMID_HIGH_THRESHOLD) / 2,
    "co2_ppm": (CO2_LOW_THRESHOLD + CO2_HIGH_THRESHOLD) / 2,
    "light_lux": LIGHT_LOW_THRESHOLD,
}

VALID_WX_DEVICES = {"heater", "fan", "led", "atomizer"}

# OLED 配置（如未定义则用默认）
OLED_BUS    = getattr(CFG, "OLED_BUS", 1)
OLED_ADDR   = getattr(CFG, "OLED_ADDR", 0x3C)
OLED_ROTATE = getattr(CFG, "OLED_ROTATE", 0)
OLED_FPS    = getattr(CFG, "OLED_FPS", 20)

# ---- 日志 ----
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("app")

# ---- 硬件 / 服务实例 ----
from actuators.atomizer import Atomizer
from services.sampler import Sampler
from services.oled import OledDisplay
from sensors.EnvironmentController import DeviceController

# 雾化器（GPIO17），注意：我们后面会用 SHT4x 的湿度自动控制它
atomizer = Atomizer(pin=ATOMIZER_PIN, active_high=ATOMIZER_ACTIVE_HIGH, initial=False)
atexit.register(atomizer.cleanup)

# 采样线程：负责从 SCD41 / SHT4x / DS18B20 / VEML7700 读取数据
sampler = Sampler(interval_sec=SAMPLE_INTERVAL_SEC)
sampler.start()

# 环境控制器：负责 Heater / Fan / LED 三个 GPIO
device_controller = DeviceController(HEATER_PIN, FAN_PIN, LED_PIN)

# OLED 显示
oled = OledDisplay(bus=OLED_BUS, addr=OLED_ADDR, rotate=OLED_ROTATE, fps=OLED_FPS)

# 微信小程序远程控制的手动指令缓存
manual_overrides = {}

# ---- Flask app ----
app = Flask(__name__, template_folder="templates", static_folder="static")


# ======================= Web 路由 =======================
@app.route("/")
def index():
    return render_template(
        "index.html",
        sample_interval=SAMPLE_INTERVAL_SEC,
        ideal_environment=IDEAL_ENVIRONMENT,
    )


@app.route("/api/data")
def api_data():
    """
    返回最新传感器数据，同时刷新 OLED 上的数值。
    """
    snap = sampler.snapshot()
    now = snap.get("now", {}) or {}

    # 刷新 OLED
    try:
        oled.show_numbers(
            co2_ppm=now.get("co2_ppm"),
            t1=now.get("temp1_c"),
            t2=now.get("temp2_c"),
            rh=now.get("rh_air"),
            light=now.get("light"),
        )
    except Exception:
        pass

    # 把雾化器当前状态也附加给前端显示
    snap["atomizer"] = atomizer.state
    return jsonify(snap)


@app.route("/api/atomizer", methods=["GET", "POST"])
def api_atomizer():
    """
    手动开关 GPIO17（雾化器）。注意：环境控制线程也会根据 SHT4x 湿度自动控制它，
    所以这里只是「临时」指令，之后可能被自动逻辑覆盖。
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        state = (data.get("state") or "").lower()
    else:
        state = (request.args.get("state") or "").lower()

    if state not in ("on", "off"):
        return jsonify(ok=False, message="use state=on|off"), 400

    try:
        atomizer.set(state == "on")
        return jsonify(ok=True, state=atomizer.state)
    except Exception as e:
        return jsonify(ok=False, message=f"hw error: {e}"), 500


@app.route("/api/oled/text")
def api_oled_text():
    """
    调试接口：在 OLED 上闪现一段文字几秒钟。
    """
    text = request.args.get("text") or ""
    if not text:
        return jsonify(ok=False, message="text required"), 400
    sec = float(request.args.get("sec") or 2)
    oled.flash(text, sec)
    return jsonify(ok=True)


def _purge_expired_overrides(now: Optional[float] = None) -> None:
    now = now or time.time()
    expired = [k for k, v in manual_overrides.items() if v.get("until", 0) <= now]
    for key in expired:
        manual_overrides.pop(key, None)


def _serialize_overrides(now: Optional[float] = None) -> dict:
    now = now or time.time()
    return {
        name: {
            "state": bool(info.get("state")),
            "seconds_left": max(0, int(info.get("until", 0) - now)),
        }
        for name, info in manual_overrides.items()
    }


def _apply_override(device: str, state: bool) -> None:
    if device == "atomizer":
        atomizer.set(state)
    elif device == "heater":
        device_controller.set_heater_manual(state)
    elif device == "fan":
        device_controller.set_fan_manual(state)
    elif device == "led":
        device_controller.set_led_manual(state)


def _apply_active_overrides(now: Optional[float] = None) -> None:
    _purge_expired_overrides(now)
    for dev, info in manual_overrides.items():
        _apply_override(dev, bool(info.get("state")))


def _current_device_states() -> dict:
    states = device_controller.as_dict()
    states["atomizer"] = atomizer.state
    return states


@app.route("/api/wx/devices", methods=["GET", "POST"])
def api_wx_devices():
    """
    微信小程序使用的远程控制接口：
      - GET  返回当前各路设备状态，以及还在生效的手动指令剩余时间；
      - POST 下发手动开关指令，可选 seconds 指定持续时间（默认 5 分钟）。
    """
    now = time.time()
    _purge_expired_overrides(now)

    if request.method == "GET":
        return jsonify(ok=True, devices=_current_device_states(), overrides=_serialize_overrides(now))

    data = request.get_json(silent=True) or {}
    dev = (data.get("device") or "").lower()
    state = data.get("state")
    duration = data.get("seconds")

    if dev not in VALID_WX_DEVICES:
        return jsonify(ok=False, message=f"device must be one of {sorted(VALID_WX_DEVICES)}"), 400
    if not isinstance(state, bool):
        return jsonify(ok=False, message="state must be boolean true/false"), 400

    try:
        seconds = float(duration) if duration is not None else float(WX_OVERRIDE_SECONDS)
    except (TypeError, ValueError):
        seconds = float(WX_OVERRIDE_SECONDS)

    seconds = max(1.0, min(seconds, float(WX_MAX_OVERRIDE_SECONDS)))
    manual_overrides[dev] = {"state": state, "until": now + seconds}

    # 立即应用一次，避免等待后台循环
    _apply_override(dev, state)

    return jsonify(ok=True, devices=_current_device_states(), overrides=_serialize_overrides(now))


# ======================= 环境控制核心逻辑 =======================
def _control_atomizer_with_sht4x(rh_air: Optional[float]) -> None:
    """
    使用 SHT4x 的相对湿度 rh_air 决定是否开启 GPIO17（雾化器）。
    规则：
      rh_air < HUMID_LOW_THRESHOLD  -> 打开雾化器 (GPIO17)
      rh_air > HUMID_HIGH_THRESHOLD -> 关闭雾化器
      中间区间保持原来的状态不变，形成一个「湿度死区」，避免频繁开关。
    """
    if rh_air is None:
        return

    try:
        rh = float(rh_air)
    except (TypeError, ValueError):
        return

    if rh < HUMID_LOW_THRESHOLD:
        atomizer.set(True)
    elif rh > HUMID_HIGH_THRESHOLD:
        atomizer.set(False)
    # 中间区间不动 atomizer.state


def control_task():
    """
    后台线程：每隔几秒读取最新样本，根据你的规则控制：
      - heater : 由 temp1_c / temp2_c 决定；
      - fan    : 由 co2_ppm 决定；
      - led    : 由 light 决定；
      - atomizer(GPIO17) : 由 SHT4x 的 rh_air 决定。
    """
    log.info("Environment control thread started")
    while True:
        snap = sampler.snapshot()
        now = snap.get("now", {}) or {}

        co2   = now.get("co2_ppm")
        t1    = now.get("temp1_c")
        t2    = now.get("temp2_c")
        light = now.get("light")
        rh    = now.get("rh_air")     # SHT4x 的湿度，用来控制 GPIO17

        # 1) Heater / Fan / LED 交给 DeviceController 处理
        device_controller.update_environment(
            temp1=t1,
            temp2=t2,
            co2_ppm=co2,
            light=light,
            temp_set=TEMP_SETPOINT,
            temp_tolerance=TEMP_TOLERANCE,
            co2_high=CO2_HIGH_THRESHOLD,
            co2_low=CO2_LOW_THRESHOLD,
            light_low=LIGHT_LOW_THRESHOLD,
        )

        # 2) 使用 SHT4x 湿度控制 GPIO17（通过 Atomizer）
        _control_atomizer_with_sht4x(rh)

        # 3) 如果有微信小程序下发的手动指令，则覆盖自动逻辑
        _apply_active_overrides()

        time.sleep(SAMPLE_INTERVAL_SEC)


# 在 Flask 第一次收到请求时启动环境控制线程
@app.before_first_request
def _start_background_threads():
    import threading
    th = threading.Thread(target=control_task, name="env-control", daemon=True)
    th.start()
    log.info("Background env-control thread started")


# ======================= main =======================
if __name__ == "__main__":
    host, port = "0.0.0.0", 5000
    log.info("Running on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False)
