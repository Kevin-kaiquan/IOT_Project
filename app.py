#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Main application entry for environment monitoring and control."""
from __future__ import annotations
import os
import sys
import time
import atexit
import logging
import random
import threading
from typing import Optional, Tuple

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

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    import config as CFG
except Exception:
    class _CFG: ...
    CFG = _CFG()

SAMPLE_INTERVAL_SEC = getattr(CFG, "SAMPLE_INTERVAL_SEC", 3)
DEFAULT_OVERRIDE_SEC = getattr(CFG, "MANUAL_OVERRIDE_SEC", 300)

TEMP_SETPOINT      = getattr(CFG, "TEMP_SETPOINT", 22.0)
TEMP_TOLERANCE     = getattr(CFG, "TEMP_TOLERANCE", 0.5)
CO2_HIGH_THRESHOLD = getattr(CFG, "CO2_HIGH_THRESHOLD", 1000.0)
CO2_LOW_THRESHOLD  = getattr(CFG, "CO2_LOW_THRESHOLD", 800.0)
LIGHT_LOW_THRESHOLD = getattr(CFG, "LIGHT_LOW_THRESHOLD", 50.0)

CO2_SAFE_TARGET = getattr(CFG, "CO2_SAFE_TARGET", 700.0)
CO2_SAFE_STOP   = getattr(CFG, "CO2_SAFE_STOP", 650.0)
CAMERA_LED_WARMUP_SEC = getattr(CFG, "CAMERA_LED_WARMUP_SEC", 0.8)
CAMERA_DETECT_MIN_SEC = getattr(CFG, "CAMERA_DETECT_MIN_SEC", 5.0)
CAMERA_DETECT_MAX_SEC = getattr(CFG, "CAMERA_DETECT_MAX_SEC", 10.0)

HUMID_LOW_THRESHOLD  = getattr(CFG, "HUMID_LOW_THRESHOLD", 55.0)
HUMID_HIGH_THRESHOLD = getattr(CFG, "HUMID_HIGH_THRESHOLD", 65.0)

IDEAL_ENVIRONMENT = {
    "temp_c": 25.5,
    "humidity": 62.5,
    "co2_ppm": 4000,
    "light_lux": 25,
    "temp_range": "24-27°C",
    "humidity_range": "60-65%RH",
    "co2_range": "3000-5000 ppm",
    "light_range": "0-50 lux",
}

OLED_BUS    = getattr(CFG, "OLED_BUS", 1)
OLED_ADDR   = getattr(CFG, "OLED_ADDR", 0x3C)
OLED_ROTATE = getattr(CFG, "OLED_ROTATE", 0)
OLED_FPS    = getattr(CFG, "OLED_FPS", 20)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("app")


class SimplePID:
    """Minimal PID controller for fan speed decisions."""

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
    """Schedule camera detections and track recent results."""

    def __init__(
        self,
        controller: "DeviceController",
        camera: "CameraManager",
        detector: Optional[TeachableMachineDetector] = None,
    ) -> None:
        self.controller = controller
        self.camera = camera
        self.detector = detector or TeachableMachineDetector(os.path.join(ROOT, "model"))
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

    def _capture_frame(self) -> Tuple[int, bytes]:
        for cam_id in self.camera.device_indices:
            try:
                return cam_id, self.camera.get_frame(cam_id)
            except Exception as e:
                log.debug(f"camera {cam_id} frame failed: {e}")
                continue
        raise RuntimeError("no camera frame available")

    def _run_model(self, frame: bytes) -> dict:
        """Invoke the local Teachable Machine model and normalize output."""
        if not getattr(self.detector, "interpreter", None):
            return {}
        try:
            result = self.detector.detect(frame)
        except Exception as exc:
            log.warning("local detection failed: %s", exc)
            return {}

        predictions = result.get("predictions") or []
        for p in predictions:
            p["confidence"] = self._normalize_conf(p.get("confidence") or 0.0)

        return {
            "predictions": predictions,
            "label": result.get("label") or "unknown",
            "probability": self._normalize_conf(result.get("probability") or 0.0),
        }

    @staticmethod
    def _normalize_conf(val: float) -> float:
        try:
            f = float(val)
        except (TypeError, ValueError):
            return 0.0
        return f / 100.0 if f > 1.0 else f

    def perform_detection(self) -> dict:
        cam_id, frame = self._capture_frame()
        data = self._run_model(frame)
        predictions = data.get("predictions") or []

        target_label = "shiitake"
        danger_labels = {"mold", "fly agaric"}

        mush_conf = 0.0
        for p in predictions:
            label = str(p.get("class") or "").lower()
            if label == target_label:
                mush_conf = max(mush_conf, self._normalize_conf(p.get("confidence")))

        contaminants = [
            {
                "name": p.get("class") or "contaminant",
                "prob": self._normalize_conf(p.get("confidence")),
            }
            for p in predictions
            if str(p.get("class") or "").lower() in danger_labels
        ]

        has_prediction = bool(predictions)
        detection_label = str(data.get("label") or "").lower() if has_prediction else ""
        detection_prob = self._normalize_conf(data.get("probability") or 0.0) if has_prediction else 0.0
        count = 1 if detection_label == target_label and detection_prob >= 0.6 else 0

        return {
            "count": count,
            "mushroom_confidence": mush_conf,
            "contaminants": contaminants,
            "predictions": predictions,
            "label": detection_label,
            "probability": detection_prob,
            "camera_id": cam_id,
            "detected": has_prediction,
            "top_prediction": predictions[0] if predictions else None,
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

from actuators.atomizer import Atomizer
from services.camera import CameraManager
from services.detector import TeachableMachineDetector
from services.sampler import Sampler
from services.oled import OledDisplay
from sensors.EnvironmentController import DeviceController

atomizer = Atomizer(pin=ATOMIZER_PIN, active_high=ATOMIZER_ACTIVE_HIGH, initial=False)
atexit.register(atomizer.cleanup)

_manual_overrides = {
}

sampler = Sampler(interval_sec=SAMPLE_INTERVAL_SEC)
sampler.start()

device_controller = DeviceController(HEATER_PIN, FAN_PIN, LED_PIN)

camera_manager = CameraManager(device_indices=[0, 1])
atexit.register(camera_manager.cleanup)

teachable_detector = TeachableMachineDetector(os.path.join(ROOT, "model"))
camera_supervisor = CameraSupervisor(device_controller, camera_manager, teachable_detector)
atexit.register(camera_supervisor.stop)

oled = OledDisplay(bus=OLED_BUS, addr=OLED_ADDR, rotate=OLED_ROTATE, fps=OLED_FPS)

app = Flask(__name__, template_folder="templates", static_folder="static")


@app.route("/")
def index():
    return render_template(
        "index.html",
        sample_interval=SAMPLE_INTERVAL_SEC,
        ideal_environment=IDEAL_ENVIRONMENT,
    )


def _cleanup_overrides() -> None:
    """Remove expired manual override instructions."""
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
    """Return the latest sensor snapshot and refresh OLED content."""
    snap = sampler.snapshot()
    now = snap.get("now", {}) or {}

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

    snap["atomizer"] = atomizer.state
    snap["devices"] = device_controller.states
    snap["camera"] = camera_supervisor.status()
    snap["co2_source"] = now.get("co2_from")
    snap["overrides"] = _serialize_overrides()
    return jsonify(snap)


@app.route("/api/camera/<int:cam_id>/frame")
def api_camera_frame(cam_id: int):
    """Return a JPEG frame for the requested camera id."""
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
    """Return readiness information for configured cameras."""
    return jsonify(ok=True, cameras=camera_manager.status())


@app.route("/api/atomizer", methods=["GET", "POST"])
def api_atomizer():
    """Temporarily toggle the atomizer via manual override."""
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
        else:
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
    """Flash short text on the OLED for debugging."""
    text = request.args.get("text") or ""
    if not text:
        return jsonify(ok=False, message="text required"), 400
    sec = float(request.args.get("sec") or 2)
    oled.flash(text, sec)
    return jsonify(ok=True)


def _control_atomizer_with_rh(rh_air: Optional[float]) -> None:
    """Toggle the atomizer based on relative humidity thresholds."""
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


def control_task():
    """Background loop that applies control rules using recent samples."""
    log.info("Environment control thread started")
    fan_pid = SimplePID(setpoint=CO2_SAFE_TARGET)
    while True:
        snap = sampler.snapshot()
        now = snap.get("now", {}) or {}

        co2   = now.get("co2_ppm")
        t1    = now.get("temp1_c")
        t2    = now.get("temp2_c")
        light = now.get("light")
        rh    = now.get("rh_air")
        mushrooms = camera_supervisor.mushroom_count

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

        heater_manual = _get_override_state("heater")
        fan_manual = _get_override_state("fan")
        led_manual = _get_override_state("led")

        if heater_manual is not None:
            device_controller.set_heater(heater_manual)
        if led_manual is not None:
            device_controller.set_led(led_manual)

        atom_manual = _get_override_state("atomizer")
        if atom_manual is not None:
            atomizer.set(atom_manual)
        else:
            _control_atomizer_with_rh(rh)

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


@app.before_first_request
def _start_background_threads():
    th = threading.Thread(target=control_task, name="env-control", daemon=True)
    th.start()
    log.info("Background env-control thread started")
    camera_supervisor.start()
    log.info("Camera supervisor thread started")


if __name__ == "__main__":
    host, port = "0.0.0.0", 5000
    log.info("Running on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False)
