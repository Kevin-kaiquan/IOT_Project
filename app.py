#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主应用 app.py

修改点（按你的最新需求）：
1. 采集部分仍由 services.sampler.Sampler 完成，不改。
2. 环境控制逻辑：
   - 加热器（继电器）由 **两个温度探针 temp1 / temp2** 决定是否开启；
   - 风扇默认由 **CO₂ 浓度** 决定；当相机识别到蘑菇后，改用 PID 以 CO₂ 目标浓度排风。
   - LED 仅在相机识别前自动点亮（补光），不再参与环境光补偿。
   - 环境湿度/温度直接由 **SCD41** 提供，湿度 rh_air 控制 GPIO17 雾化器。

网页 /api/data 和 /api/atomizer 的接口保持不变，这样你的前端 dashboard 可以继续使用。
"""
import os
import sys
import time
import atexit
import logging
import random
import threading
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
DEFAULT_OVERRIDE_SEC = getattr(CFG, "MANUAL_OVERRIDE_SEC", 300)

# 环境控制用的 setpoint / 阈值（如未在 config.py 里定义，则用默认值）
TEMP_SETPOINT      = getattr(CFG, "TEMP_SETPOINT", 22.0)   # 加热目标温度（°C）
TEMP_TOLERANCE     = getattr(CFG, "TEMP_TOLERANCE", 0.5)   # 温度允许波动
CO2_HIGH_THRESHOLD = getattr(CFG, "CO2_HIGH_THRESHOLD", 1000.0)  # CO₂ 打开风扇阈值
CO2_LOW_THRESHOLD  = getattr(CFG, "CO2_LOW_THRESHOLD", 800.0)    # CO₂ 关闭风扇阈值
LIGHT_LOW_THRESHOLD = getattr(CFG, "LIGHT_LOW_THRESHOLD", 50.0)  # lux，低于此值就点亮 LED

# 相机 & 风扇 PID 相关
CO2_SAFE_TARGET = getattr(CFG, "CO2_SAFE_TARGET", 700.0)
CO2_SAFE_STOP   = getattr(CFG, "CO2_SAFE_STOP", 650.0)
CAMERA_LED_WARMUP_SEC = getattr(CFG, "CAMERA_LED_WARMUP_SEC", 0.8)
CAMERA_DETECT_MIN_SEC = getattr(CFG, "CAMERA_DETECT_MIN_SEC", 5.0)
CAMERA_DETECT_MAX_SEC = getattr(CFG, "CAMERA_DETECT_MAX_SEC", 10.0)

# 环境湿度（来自 SCD41）控制 GPIO17（雾化器 / 喷雾器）用的湿度阈值
HUMID_LOW_THRESHOLD  = getattr(CFG, "HUMID_LOW_THRESHOLD", 55.0)
HUMID_HIGH_THRESHOLD = getattr(CFG, "HUMID_HIGH_THRESHOLD", 65.0)

# 目标生长环境（用于前端差异提示）
IDEAL_ENVIRONMENT = {
    # 参考菌丝阶段（Mycelial Run）的理想区间
    "temp_c": 25.5,  # 24-27°C 区间中值
    "humidity": 62.5,  # 60-65%RH 区间中值
    "co2_ppm": 4000,  # 理想 3000-5000 ppm
    "light_lux": 25,  # 0-50 lux
    "temp_range": "24-27°C",
    "humidity_range": "60-65%RH",
    "co2_range": "3000-5000 ppm",
    "light_range": "0-50 lux",
}

# OLED 配置（如未定义则用默认）
OLED_BUS    = getattr(CFG, "OLED_BUS", 1)
OLED_ADDR   = getattr(CFG, "OLED_ADDR", 0x3C)
OLED_ROTATE = getattr(CFG, "OLED_ROTATE", 0)
OLED_FPS    = getattr(CFG, "OLED_FPS", 20)

# ---- 日志 ----
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("app")


class SimplePID:
    """简易 PID，用于风扇按 CO₂ 目标控制。

    输出是一个无量纲的数值；在当前项目中，只需要判断是否 >0 来决定是否拉高风扇继电器。
    """

    def __init__(
        self,
        kp: float = 0.8,
        ki: float = 0.05,
        kd: float = 0.1,
        setpoint: float = CO2_SAFE_TARGET,
        output_limits: tuple[Optional[float], Optional[float]] = (-1.0, 1.0),
    ) -> None:
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.setpoint = setpoint
        self.output_limits = output_limits
        self._integral = 0.0
        self._last_error: Optional[float] = None

    def reset(self) -> None:
        self._integral = 0.0
        self._last_error = None

    def step(self, measurement: float, dt: float) -> float:
        error = float(measurement) - float(self.setpoint)
        self._integral += error * dt
        derivative = 0.0
        if self._last_error is not None and dt > 0:
            derivative = (error - self._last_error) / dt
        self._last_error = error

        output = self.kp * error + self.ki * self._integral + self.kd * derivative
        lo, hi = self.output_limits
        if lo is not None:
            output = max(lo, output)
        if hi is not None:
            output = min(hi, output)
        return output


class CameraSupervisor:
    """
    负责：
    - 按 5~10s 周期自动点亮 LED → 运行识别 → 关闭 LED
    - 记录最近一次识别的蘑菇数量（供风扇 PID 使用）

    识别本身在 perform_detection() 里留了扩展口，当前作为占位返回现有计数。
    """

    def __init__(self, controller: "DeviceController") -> None:
        self.controller = controller
        self.mushroom_count = 0
        self.last_detection_ts: Optional[float] = None
        self.last_detection_result: dict = {
            "mushroom_confidence": 0.0,
            "contaminants": [],
        }
        self._stop_evt = threading.Event()
        self._th = threading.Thread(target=self._loop, name="camera-supervisor", daemon=True)

    def start(self) -> None:
        if not self._th.is_alive():
            self._th.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._th.join(timeout=1.0)

    def perform_detection(self) -> dict:
        """
        真正的图像识别逻辑可以替换这里。
        返回 dict，至少包含 count（识别出的目标蘑菇数量）。
        """
        # 这里仍然是占位逻辑：模拟一个置信度并携带常见污染物概率，便于前端展示。
        mushroom_confidence = round(random.uniform(0.6, 0.98), 2)
        contaminants = [
            {"name": "绿霉", "prob": round(random.uniform(0.05, 0.22), 2)},
            {"name": "黑曲霉", "prob": round(random.uniform(0.0, 0.12), 2)},
        ]
        return {
            "count": 1 if mushroom_confidence >= 0.75 else 0,
            "mushroom_confidence": mushroom_confidence,
            "contaminants": contaminants,
        }

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            interval = random.uniform(CAMERA_DETECT_MIN_SEC, CAMERA_DETECT_MAX_SEC)
            self._stop_evt.wait(interval)
            if self._stop_evt.is_set():
                break
            try:
                self.controller.set_led(True)
                time.sleep(CAMERA_LED_WARMUP_SEC)
                result = self.perform_detection() or {}
                self.mushroom_count = max(0, int(result.get("count") or 0))
                self.last_detection_result = result
                self.last_detection_ts = time.time()
            except Exception as e:
                log.warning(f"camera detection loop error: {e}")
            finally:
                self.controller.set_led(False)

    def status(self) -> dict:
        return {
            "mushroom_count": self.mushroom_count,
            "last_detection_ts": self.last_detection_ts,
            "led": "on" if self.controller.led_on else "off",
            "detection": self.last_detection_result,
        }

# ---- 硬件 / 服务实例 ----
from actuators.atomizer import Atomizer
from services.camera import CameraManager
from services.sampler import Sampler
from services.oled import OledDisplay
from sensors.EnvironmentController import DeviceController

# 雾化器（GPIO17），注意：我们用 SCD41 的湿度自动控制它
atomizer = Atomizer(pin=ATOMIZER_PIN, active_high=ATOMIZER_ACTIVE_HIGH, initial=False)
atexit.register(atomizer.cleanup)

# 远程控制（含小程序）用的“临时强制状态”缓存
_manual_overrides = {
    # name -> {"on": bool, "expires_at": float}
}

# 采样线程：负责从 SCD41 / DS18B20 / VEML7700 读取数据
sampler = Sampler(interval_sec=SAMPLE_INTERVAL_SEC)
sampler.start()

# 环境控制器：负责 Heater / Fan / LED 三个 GPIO
device_controller = DeviceController(HEATER_PIN, FAN_PIN, LED_PIN)

# 相机 LED/检测调度器
camera_supervisor = CameraSupervisor(device_controller)
atexit.register(camera_supervisor.stop)

# 相机采集（最多两个 USB 摄像头）
camera_manager = CameraManager(device_indices=[0, 1])
atexit.register(camera_manager.cleanup)

# OLED 显示
oled = OledDisplay(bus=OLED_BUS, addr=OLED_ADDR, rotate=OLED_ROTATE, fps=OLED_FPS)

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


def _cleanup_overrides() -> None:
    """清理过期的手动指令。"""
    now = time.time()
    for name, info in list(_manual_overrides.items()):
        exp = info.get("expires_at")
        if exp is not None and exp < now:
            _manual_overrides.pop(name, None)


def _get_override_state(name: str) -> Optional[bool]:
    _cleanup_overrides()
    info = _manual_overrides.get(name)
    if not info:
        return None
    return bool(info.get("on"))


def _set_override(name: str, on: bool, duration_sec: float = DEFAULT_OVERRIDE_SEC) -> None:
    expires_at = time.time() + float(duration_sec) if duration_sec else None
    _manual_overrides[name] = {"on": bool(on), "expires_at": expires_at}


def _serialize_overrides() -> dict:
    _cleanup_overrides()
    result = {}
    for k, v in _manual_overrides.items():
        result[k] = {
            "state": "on" if v.get("on") else "off",
            "expires_at": v.get("expires_at"),
        }
    return result


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

    # 把雾化器、设备、相机状态也附加给前端显示
    snap["atomizer"] = atomizer.state
    snap["devices"] = device_controller.states
    snap["camera"] = camera_supervisor.status()
    snap["co2_source"] = now.get("co2_from")
    snap["overrides"] = _serialize_overrides()
    return jsonify(snap)


@app.route("/api/camera/<int:cam_id>/frame")
def api_camera_frame(cam_id: int):
    """返回指定摄像头的 JPEG 帧（camera id 由操作系统分配，通常是 0 或 1）。"""
    try:
        frame = camera_manager.get_frame(cam_id)
    except KeyError:
        return jsonify(ok=False, message="invalid camera id"), 404
    except Exception as e:
        return jsonify(ok=False, message=str(e)), 503

    resp = app.response_class(frame, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/camera/status")
def api_camera_status():
    """简单返回两个摄像头的就绪情况，便于前端判断是否有画面。"""
    return jsonify(ok=True, cameras=camera_manager.status())


@app.route("/api/atomizer", methods=["GET", "POST"])
def api_atomizer():
    """
    手动开关 GPIO17（雾化器）。注意：环境控制线程也会根据 SCD41 湿度自动控制它，
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


@app.route("/api/control", methods=["GET", "POST"])
def api_control():
    """
    供微信小程序或其他客户端使用的统一控制接口。

    - GET 返回当前设备状态 + 手动指令剩余时间
    - POST 接受 {device, state, duration_sec?}，会在 duration 内保持指定状态
    """
    if request.method == "GET":
        return jsonify(
            ok=True,
            devices={**device_controller.states, "atomizer": atomizer.state},
            overrides=_serialize_overrides(),
        )

    data = request.get_json(silent=True) or {}
    device = (data.get("device") or "").lower()
    state = (data.get("state") or "").lower()
    duration = float(data.get("duration_sec") or DEFAULT_OVERRIDE_SEC)

    if device not in {"heater", "fan", "led", "atomizer"}:
        return jsonify(ok=False, message="device must be heater|fan|led|atomizer"), 400
    if state not in {"on", "off"}:
        return jsonify(ok=False, message="state must be on|off"), 400

    try:
        is_on = state == "on"
        _set_override(device, is_on, duration)

        if device == "heater":
            device_controller.set_heater(is_on)
        elif device == "fan":
            device_controller.set_fan(is_on)
        elif device == "led":
            device_controller.set_led(is_on)
        else:  # atomizer
            atomizer.set(is_on)

        return jsonify(
            ok=True,
            device=device,
            state=state,
            duration_sec=duration,
            overrides=_serialize_overrides(),
        )
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


# ======================= 环境控制核心逻辑 =======================
def _control_atomizer_with_rh(rh_air: Optional[float]) -> None:
    """
    使用环境相对湿度 rh_air 决定是否开启 GPIO17（雾化器）。
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
    后台线程：每隔几秒读取最新样本，根据规则控制：
      - heater : 由 temp1_c / temp2_c 决定；
      - fan    : 当识别到蘑菇时采用 PID 将 CO₂ 拉到安全值；否则使用滞回阈值；
      - led    : 不再跟随光照；由 CameraSupervisor 在识别前点亮；
      - atomizer(GPIO17) : 由 SCD41 的 rh_air 决定。
    """
    log.info("Environment control thread started")
    fan_pid = SimplePID(setpoint=CO2_SAFE_TARGET)
    while True:
        snap = sampler.snapshot()
        now = snap.get("now", {}) or {}

        co2   = now.get("co2_ppm")
        t1    = now.get("temp1_c")
        t2    = now.get("temp2_c")
        light = now.get("light")
        rh    = now.get("rh_air")     # SCD41 的湿度，用来控制 GPIO17
        mushrooms = camera_supervisor.mushroom_count

        # 1) Heater 按探针控制；Fan 仅在“无识别”模式下由阈值控制
        manage_fan = mushrooms <= 0
        device_controller.update_environment(
            temp1=t1,
            temp2=t2,
            co2_ppm=co2 if manage_fan else None,
            light=light,
            temp_set=TEMP_SETPOINT,
            temp_tolerance=TEMP_TOLERANCE,
            co2_high=CO2_HIGH_THRESHOLD,
            co2_low=CO2_LOW_THRESHOLD,
            manage_fan=manage_fan,
        )

        # 如果有手动覆盖指令，优先执行
        heater_manual = _get_override_state("heater")
        fan_manual = _get_override_state("fan")
        led_manual = _get_override_state("led")

        if heater_manual is not None:
            device_controller.set_heater(heater_manual)
        if led_manual is not None:
            device_controller.set_led(led_manual)

        # 2) 使用 SCD41 湿度控制 GPIO17（通过 Atomizer）
        atom_manual = _get_override_state("atomizer")
        if atom_manual is not None:
            atomizer.set(atom_manual)
        else:
            _control_atomizer_with_rh(rh)

        # 3) 风扇：若识别到蘑菇，用 PID 将 CO2 拉到安全值；否则沿用阈值逻辑
        if fan_manual is not None:
            device_controller.set_fan(fan_manual)
        elif mushrooms > 0 and co2 is not None:
            output = fan_pid.step(co2, SAMPLE_INTERVAL_SEC)
            if co2 <= CO2_SAFE_STOP or mushrooms <= 0:
                device_controller.set_fan(False)
            else:
                device_controller.set_fan(output > 0)
        else:
            fan_pid.reset()

        time.sleep(SAMPLE_INTERVAL_SEC)


# 在 Flask 第一次收到请求时启动环境控制线程
@app.before_first_request
def _start_background_threads():
    th = threading.Thread(target=control_task, name="env-control", daemon=True)
    th.start()
    log.info("Background env-control thread started")
    camera_supervisor.start()
    log.info("Camera supervisor thread started")


# ======================= main =======================
if __name__ == "__main__":
    host, port = "0.0.0.0", 5000
    log.info("Running on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False)
