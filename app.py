#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Main application entry for environment monitoring and control."""
import os
import re
import sys
import time
import base64
import atexit
import logging
import random
import threading
from typing import Optional, Tuple, Union

import requests
import cv2  # type: ignore
import numpy as np

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

try:
    from inference_sdk import InferenceHTTPClient
except Exception:
    InferenceHTTPClient = None

try:
    from inference import InferencePipeline  # type: ignore
except Exception:
    InferencePipeline = None

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

def _parse_model_id(model_id: str) -> tuple[str, str, str]:
    raw = (model_id or "").strip().strip("/")
    if not raw:
        return "", "", ""

    workspace = ""
    model = raw
    if "/" in raw:
        workspace, model = raw.split("/", 1)

    version = ""
    if "/" in model:
        model, version = model.rsplit("/", 1)
    else:
        match = re.search(r"-([0-9]+)$", model)
        if match:
            version = match.group(1)
            model = model[: -len(match.group(0))]

    return workspace, model, version


ROBOFLOW_MODEL_ID = os.getenv("ROBOFLOW_MODEL_ID", "kevin-stoob/mushroom_demo-gkc1f/2")
ROBOFLOW_MODEL_URL = os.getenv("ROBOFLOW_MODEL_URL", "mushroom_demo-gkc1f/2").strip("/")
_model_workspace, _model_slug, _model_version = _parse_model_id(ROBOFLOW_MODEL_ID)
ROBOFLOW_API_KEY = os.getenv("ROBOFLOW_API_KEY", "OVH73o1hdgSYepnRlv4U")
ROBOFLOW_WORKSPACE = os.getenv("ROBOFLOW_WORKSPACE", _model_workspace or "kevin-stoob")
ROBOFLOW_MODEL_VERSION = os.getenv("ROBOFLOW_MODEL_VERSION", _model_version or "2")
ROBOFLOW_MODEL_PATH = "/".join(
    part.strip("/")
    for part in (ROBOFLOW_WORKSPACE if _model_workspace else "", _model_slug, ROBOFLOW_MODEL_VERSION)
    if part
)
ROBOFLOW_BASE_URL = f"https://detect.roboflow.com/{ROBOFLOW_MODEL_URL}"
ROBOFLOW_CLIENT = (
    InferenceHTTPClient(api_url="https://detect.roboflow.com", api_key=ROBOFLOW_API_KEY)
    if InferenceHTTPClient is not None
    else None
)
ROBOFLOW_WORKFLOW_ID = os.getenv("ROBOFLOW_WORKFLOW_ID", "mushroom")
ROBOFLOW_PIPELINE_VIDEO = os.getenv("ROBOFLOW_PIPELINE_VIDEO", "0")

TARGET_CLASS = "shitake mushroom"
ALIEN_CLASS = "button mushroom"

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


class RoboflowWorkflowPipeline:
    """Optional Roboflow streaming pipeline to keep workflow detections alive."""

    def __init__(self, video_reference: str | int = ROBOFLOW_PIPELINE_VIDEO, max_fps: int = 10) -> None:
        try:
            self.video_reference: str | int = int(str(video_reference).strip())
        except Exception:
            self.video_reference = video_reference
        self.max_fps = max_fps
        self._pipeline = None
        self._th: Optional[threading.Thread] = None
        self._latest: Optional[dict] = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        if InferencePipeline is None:
            return False
        if self._pipeline is not None:
            return True

        try:
            self._pipeline = InferencePipeline.init_with_workflow(
                api_key=ROBOFLOW_API_KEY,
                workspace_name=ROBOFLOW_WORKSPACE,
                workflow_id=ROBOFLOW_WORKFLOW_ID,
                video_reference=self.video_reference,
                max_fps=self.max_fps,
                on_prediction=self._on_prediction,
            )
            log.info(
                "Roboflow workflow pipeline initialized for %s (fps=%s)",
                self.video_reference,
                self.max_fps,
            )
        except Exception as e:
            log.warning(f"Roboflow workflow pipeline unavailable: {e}")
            self._pipeline = None
            return False

        self._th = threading.Thread(target=self._run, name="roboflow-workflow", daemon=True)
        self._th.start()
        return True

    def _run(self) -> None:
        if self._pipeline is None:
            return
        try:
            self._pipeline.start()
            self._pipeline.join()
        except Exception as e:
            log.warning(f"Roboflow workflow pipeline stopped: {e}")

    def _on_prediction(self, result, video_frame) -> None:
        payload = {"result": result, "ts": time.time()}
        if video_frame is not None:
            payload["camera_id"] = getattr(video_frame, "video_reference", 0)
        with self._lock:
            self._latest = payload

    def latest(self) -> Optional[dict]:
        with self._lock:
            if self._latest is None:
                return None
            return dict(self._latest)

    def stop(self) -> None:
        try:
            if self._pipeline is not None:
                self._pipeline.stop()
                self._pipeline.join(timeout=1)
        except Exception:
            pass


class CameraSupervisor:
    """Schedule camera detections and track recent results."""

    def __init__(self, controller: "DeviceController", camera: "CameraManager") -> None:
        self.controller = controller
        self.camera = camera
        self.mushroom_count = 0
        self.last_detection_ts: Optional[float] = None
        self.last_detection_result: dict = {
            "mushroom_confidence": 0.0,
            "contaminants": [],
            "has_predictions": False,
        }
        self.pipeline = RoboflowWorkflowPipeline()
        self._stop_evt = threading.Event()
        self._th = threading.Thread(target=self._loop, name="camera-supervisor", daemon=True)

    def start(self) -> None:
        self.pipeline.start()
        if not self._th.is_alive():
            self._th.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._th.join(timeout=1.0)
        self.pipeline.stop()

    def _capture_frame(self) -> Tuple[int, bytes]:
        for cam_id in self.camera.device_indices:
            try:
                return cam_id, self.camera.get_frame(cam_id)
            except Exception as e:
                log.debug(f"camera {cam_id} frame failed: {e}")
                continue
        raise RuntimeError("no camera frame available")

    @staticmethod
    def _extract_predictions_from_pipeline(result: dict) -> list:
        if not isinstance(result, dict):
            return []

        def _maybe_list(val):
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                nested = val.get("predictions")
                if isinstance(nested, list):
                    return nested
            return None

        # Direct predictions
        direct = _maybe_list(result.get("predictions"))
        if direct is not None:
            return direct

        # Nested results payloads
        results = result.get("results")
        if isinstance(results, dict):
            nested = _maybe_list(results)
            if nested is not None:
                return nested
        if isinstance(results, list):
            for item in results:
                nested = _maybe_list(item)
                if nested:
                    return nested

        # Workflow output container
        output_image = result.get("output") or result.get("output_image")
        if isinstance(output_image, dict):
            nested = _maybe_list(output_image)
            if nested is not None:
                return nested

        return []

    def _send_to_roboflow(self, frame: bytes) -> dict:
        if not frame:
            raise RuntimeError("empty frame captured")

        if ROBOFLOW_CLIENT is not None:
            try:
                infer_workflow = getattr(ROBOFLOW_CLIENT, "infer_from_workflow", None)
                if callable(infer_workflow):
                    return infer_workflow(
                        workflow_id=ROBOFLOW_WORKFLOW_ID,
                        image=frame,
                        workspace=ROBOFLOW_WORKSPACE,
                    ) or {}

                np_img = np.frombuffer(frame, dtype=np.uint8)
                img = cv2.imdecode(np_img, cv2.IMREAD_COLOR)
                if img is None:
                    raise RuntimeError("camera frame decode failed")
                return ROBOFLOW_CLIENT.infer(img, model_id=ROBOFLOW_MODEL_PATH) or {}
            except Exception as e:
                log.warning(f"Roboflow SDK inference failed, falling back to HTTP: {e}")

        # Roboflow detect endpoint (raw base64 body matches official curl flow)
        last_error: Optional[Exception] = None
        b64_body = base64.b64encode(frame).decode("utf-8", "ignore")

        # Try the documented raw base64 body
        try:
            resp = requests.post(
                ROBOFLOW_BASE_URL,
                params={"api_key": ROBOFLOW_API_KEY, "format": "json", "name": "frame.jpg"},
                data=b64_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=12,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("predictions"):
                return data
        except Exception as e:
            last_error = e
            log.warning("Roboflow base64 detect failed, retrying with form payload: %s", e)

        # Try base64 via the explicit "image" form field the docs mention
        try:
            form_resp = requests.post(
                ROBOFLOW_BASE_URL,
                params={"api_key": ROBOFLOW_API_KEY, "format": "json", "name": "frame.jpg"},
                data={"image": b64_body},
                timeout=12,
            )
            form_resp.raise_for_status()
            form_data = form_resp.json()
            if form_data.get("predictions"):
                return form_data
            last_error = last_error or Exception("no predictions from form base64 upload")
        except Exception as e:
            last_error = e
            log.warning("Roboflow form-base64 detect failed, retrying with multipart: %s", e)

        # If base64 fails or returns empty predictions, retry with multipart upload
        alt_resp = requests.post(
            ROBOFLOW_BASE_URL,
            params={"api_key": ROBOFLOW_API_KEY, "format": "json", "name": "frame.jpg"},
            files={"file": ("frame.jpg", frame, "image/jpeg")},
            timeout=12,
        )
        try:
            alt_resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(
                f"roboflow multipart http error: {e} | base64_error={last_error} | body={alt_resp.text}"
            )
        data = alt_resp.json()
        if not data.get("predictions") and last_error is not None:
            log.warning("Roboflow detect returned no predictions; base64_error=%s | body=%s", last_error, data)
        elif not data.get("predictions"):
            log.warning("Roboflow detect returned no predictions for the current frame; body=%s", data)
        return data

    @staticmethod
    def _normalize_conf(val: float) -> float:
        try:
            f = float(val)
        except (TypeError, ValueError):
            return 0.0
        return f / 100.0 if f > 1.0 else f

    def _summarize_predictions(self, predictions: list, cam_id: Union[int, str]) -> dict:
        def _class_name(p: dict) -> str:
            return str(p.get("class") or "").strip().lower()

        target_preds = [p for p in predictions if _class_name(p) == TARGET_CLASS]
        alien_preds = [p for p in predictions if _class_name(p) == ALIEN_CLASS]

        target_conf = max(
            (self._normalize_conf(p.get("confidence")) for p in target_preds),
            default=0.0,
        )
        alien_conf = max(
            (self._normalize_conf(p.get("confidence")) for p in alien_preds),
            default=0.0,
        )

        contaminants = [
            {
                "name": p.get("class") or "contaminant",
                "prob": self._normalize_conf(p.get("confidence")),
            }
            for p in predictions
            if p not in target_preds and p not in alien_preds
        ]

        return {
            "count": len(target_preds) + len(alien_preds),
            "target_count": len(target_preds),
            "target_confidence": target_conf,
            "alien_count": len(alien_preds),
            "alien_confidence": alien_conf,
            "contaminants": contaminants,
            "predictions": predictions,
            "camera_id": cam_id,
            "has_predictions": bool(predictions),
        }

    def perform_detection(self) -> dict:
        cam_id, frame = self._capture_frame()
        data = self._send_to_roboflow(frame)
        predictions = data.get("predictions") or []
        return self._summarize_predictions(predictions, cam_id)

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            interval = random.uniform(CAMERA_DETECT_MIN_SEC, CAMERA_DETECT_MAX_SEC)
            self._stop_evt.wait(interval)
            if self._stop_evt.is_set():
                break
            try:
                self.controller.set_led(True)
                time.sleep(CAMERA_LED_WARMUP_SEC)
                pipeline_result = self.pipeline.latest()
                if pipeline_result:
                    preds = self._extract_predictions_from_pipeline(
                        pipeline_result.get("result") or {}
                    )
                    if preds:
                        result = self._summarize_predictions(
                            preds, pipeline_result.get("camera_id", 0)
                        )
                    else:
                        result = self.perform_detection() or {}
                else:
                    result = self.perform_detection() or {}
                self.mushroom_count = max(0, int(result.get("target_count") or 0))
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

camera_manager = CameraManager(device_indices=[0])
atexit.register(camera_manager.cleanup)

camera_supervisor = CameraSupervisor(device_controller, camera_manager)
atexit.register(camera_supervisor.stop)
camera_supervisor.start()

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
