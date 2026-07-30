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
BACKGROUND_SERVICES_ENABLED = os.getenv("IOT_DISABLE_BACKGROUND", "").lower() not in {
    "1",
    "true",
    "yes",
}

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

GROWTH_PHASE_RULES = {
    "unknown": {"co2_target": CO2_SAFE_TARGET, "co2_low": CO2_SAFE_STOP, "co2_high": CO2_HIGH_THRESHOLD},
    "mycelium": {"co2_target": 900.0, "co2_low": 800.0, "co2_high": 1000.0},
    "fruiting": {"co2_target": 750.0, "co2_low": 650.0, "co2_high": 900.0},
    "harvest": {"co2_target": 500.0, "co2_low": 480.0, "co2_high": 520.0, "ventilate": True},
}

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


class GrowthPhaseTracker:
    """Track growth phase transitions based on detection labels."""

    def __init__(self) -> None:
        self.phase: str = "unknown"
        self.shiitake_streak: int = 0

    @staticmethod
    def _normalize_label(label: Optional[str]) -> str:
        return str(label or "").strip().lower()

    def update(self, label: Optional[str]) -> None:
        normalized = self._normalize_label(label)
        shiitake_labels = {"shiitake", "shitake"}

        if normalized in shiitake_labels:
            self.shiitake_streak += 1
            self.phase = "harvest" if self.shiitake_streak >= 5 else "fruiting"
        elif normalized == "base":
            self.phase = "mycelium"
            self.shiitake_streak = 0
        elif normalized in {"mold", "fly agaric"}:
            self.shiitake_streak = 0
        else:
            self.shiitake_streak = 0
            if normalized:
                self.phase = "unknown"

    def as_dict(self) -> dict:
        return {"phase": self.phase, "shiitake_streak": self.shiitake_streak}


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
        self.growth_tracker = GrowthPhaseTracker()
        self._stop_evt = threading.Event()
        self._th = threading.Thread(target=self._loop, name="camera-supervisor", daemon=True)

    def start(self) -> None:
        if not self._th.is_alive():
            self._th.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._th.is_alive():
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

        target_labels = {"shiitake", "shitake"}
        danger_labels = {"mold", "fly agaric"}

        mush_conf = 0.0
        for p in predictions:
            label = str(p.get("class") or "").lower()
            if label in target_labels:
                mush_conf = max(mush_conf, self._normalize_conf(p.get("confidence")))

        contaminants = [
            {
                "name": p.get("class") or "contaminant",
                "prob": self._normalize_conf(p.get("confidence")),
            }
            for p in predictions
            if str(p.get("class") or "").lower() in danger_labels
        ]

        detection_label = str(data.get("label") or "unknown").lower()
        detection_prob = self._normalize_conf(data.get("probability") or 0.0)
        count = 1 if detection_label in target_labels and detection_prob >= 0.6 else 0

        self.growth_tracker.update(detection_label)
        growth_phase = self.growth_tracker.as_dict()

        return {
            "count": count,
            "mushroom_confidence": mush_conf,
            "contaminants": contaminants,
            "predictions": predictions,
            "label": detection_label,
            "probability": detection_prob,
            "camera_id": cam_id,
            "growth_phase": growth_phase,
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
            "growth_phase": self.growth_tracker.as_dict(),
        }

from actuators.atomizer import Atomizer
from services.camera import CameraManager
from services.detector import TeachableMachineDetector
from services.sampler import Sampler
from sensors.EnvironmentController import DeviceController

try:
    from services.oled import OledDisplay
except Exception as exc:
    OledDisplay = None
    log.warning("OLED support unavailable: %s", exc)

atomizer = Atomizer(pin=ATOMIZER_PIN, active_high=ATOMIZER_ACTIVE_HIGH, initial=False)
atexit.register(atomizer.cleanup)

_manual_overrides = {
}

sampler = Sampler(interval_sec=SAMPLE_INTERVAL_SEC)
sampler.start()
atexit.register(sampler.stop)

device_controller = DeviceController(HEATER_PIN, FAN_PIN, LED_PIN)
atexit.register(device_controller.cleanup)

camera_manager = CameraManager(device_indices=[0, 1])
atexit.register(camera_manager.cleanup)

teachable_detector = TeachableMachineDetector(os.path.join(ROOT, "model"))
camera_supervisor = CameraSupervisor(device_controller, camera_manager, teachable_detector)
atexit.register(camera_supervisor.stop)

oled = None
if getattr(CFG, "OLED_ENABLE", True) and OledDisplay is not None:
    try:
        oled = OledDisplay(bus=OLED_BUS, addr=OLED_ADDR, rotate=OLED_ROTATE, fps=OLED_FPS)
        atexit.register(oled.stop)
    except Exception as exc:
        log.warning("OLED initialization failed; continuing without display: %s", exc)

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
    Unified control endpoint for the dashboard and other clients.

    GET returns current device and manual override state.
    POST accepts {device, state, duration_sec?} and holds the requested state.
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
    if oled is None:
        return jsonify(ok=False, message="OLED is disabled or unavailable"), 503
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
        growth_phase = camera_supervisor.growth_tracker.phase
        phase_rules = GROWTH_P…15841 tokens truncated… Sampler ─> Flask API ─> Browser dashboard
VEML7700┘      │              └─> OLED
               └─> CSV history

USB cameras ─> OpenCV ─> TFLite classifier ─> Growth phase
                                                │
Sensor values + growth phase ─> Control rules ─> GPIO relays
```

## Hardware

The default configuration targets a Raspberry Pi using BCM pin numbering.

| Part | Interface / default |
| --- | --- |
| Heater relay | GPIO 27 |
| Fan relay | GPIO 22 |
| Camera LED relay | GPIO 23 |
| Atomizer relay | GPIO 17, active-low |
| SCD41 | I²C bus 1, address `0x62` or `0x64` |
| VEML7700 | I²C bus 1, address `0x10` |
| OLED | I²C bus 1, address `0x3C` |
| DS18B20 probes | Linux 1-Wire, devices beginning with `28-` |
| Cameras | USB camera indices 0 and 1 |

> [!WARNING]
> Relays may switch mains-powered heaters, fans, or humidifiers. Use an
> appropriately rated, isolated relay module and have mains wiring completed by
> a qualified person. Verify active-high/active-low behavior before attaching a
> load.

## Software prerequisites

- Raspberry Pi OS Bookworm (64-bit recommended)
- Python 3.11
- I²C and 1-Wire enabled in `raspi-config`
- A Teachable Machine TFLite model if vision detection is required
- Internet access for the dashboard's Chart.js CDN, unless Chart.js is hosted
  locally

## Installation

```bash
git clone https://github.com/Kevin-kaiquan/IOT_Project.git
cd IOT_Project

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On Raspberry Pi OS, ensure the current user can access GPIO, I²C, 1-Wire, and
video devices. A reboot is normally required after enabling hardware
interfaces.

## Vision model

Create a `model/` directory and place these exported Teachable Machine files
inside it:

```text
model/
├── model.tflite
└── labels.txt
```

Expected labels include `shiitake` (the common misspelling `shitake` is also
accepted), `base`, `mold`, and `fly agaric`. The application still starts if
the model is absent, but classification remains unavailable. See
[`model/README.md`](model/README.md) for details.

## Configuration

Edit [`config.py`](config.py) before connecting loads. Important settings
include:

- GPIO pins and atomizer polarity
- sampling and camera-detection intervals
- sensor I²C addresses
- CO₂ targets and stop thresholds
- history length and CSV output directory
- OLED enablement

The growth-phase control table is currently defined in `app.py`:

| Phase | Detection | CO₂ behavior |
| --- | --- | --- |
| Unknown | no stable label | targets about 700 ppm |
| Mycelium | `base` | targets about 900 ppm |
| Fruiting | fewer than five consecutive shiitake results | targets about 750 ppm |
| Harvest | five or more consecutive shiitake results | ventilates toward about 500 ppm |

These values are project defaults, not universal cultivation advice. Validate
them for your mushroom strain, room, and equipment.

## Run

```bash
python app.py
```

Open `http://<raspberry-pi-ip>:5000` from a device on the same network. The
server listens on all interfaces and the control API has no authentication, so
do not expose port 5000 directly to the public internet.

The application records sensor samples in `history_data/`. An active file ends
with `_active.csv`; it is renamed to `_complete.csv` during a clean shutdown.

For dashboard/API development without starting the control and camera workers:

```bash
IOT_DISABLE_BACKGROUND=1 flask --app app run
```

## HTTP API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/data` | Latest readings, in-memory history, device states, overrides, and vision status |
| `GET` | `/api/camera/status` | Camera readiness |
| `GET` | `/api/camera/<id>/frame` | Latest JPEG frame |
| `GET` | `/api/control` | Device and override states |
| `POST` | `/api/control` | Set a temporary manual override |
| `GET` / `POST` | `/api/atomizer` | Direct atomizer state change |
| `GET` | `/api/oled/text?text=Hello&sec=2` | Temporarily show OLED text |

Example:

```bash
curl -X POST http://raspberrypi.local:5000/api/control \
  -H "Content-Type: application/json" \
  -d '{"device":"fan","state":"on","duration_sec":300}'
```

Request and response examples are documented in [`docs/API.md`](docs/API.md).

## Project layout

```text
app.py                    Flask routes and control orchestration
config.py                 Hardware and runtime defaults
actuators/                GPIO output drivers
sensors/                  SCD41, VEML7700, and DS18B20 drivers
services/                 Camera, TFLite, sampling, and OLED services
templates/index.html      Browser dashboard
scripts/                  Hardware self-test utilities
docs/API.md               HTTP API reference
model/README.md           Local classifier setup
```

## Hardware self-tests

Run only the test that matches the connected hardware:

```bash
python scripts/relay_selftest.py
python scripts/temperature_selftest.py
python scripts/oled_selftest.py
```

The relay test changes output states. Read
[`scripts/README.md`](scripts/README.md) before running it.

## Troubleshooting

- **No I²C devices:** run `i2cdetect -y 1`, check wiring, and enable I²C.
- **No DS18B20 devices:** verify 1-Wire is enabled and check
  `/sys/bus/w1/devices/28-*`.
- **No camera frame:** check `ls /dev/video*`, USB power, and camera permissions.
- **No classifications:** confirm `model.tflite` and `labels.txt` exist and
  install a TFLite runtime compatible with the Pi's Python version.
- **OLED unavailable:** confirm address `0x3C`, or set `OLED_ENABLE = False` in
  `config.py`.
- **Mock CO₂ values:** the sampler uses mock CO₂ data when the SCD41 cannot be
  read; inspect the application log for the underlying sensor error.

## Status

This is a prototype and educational project, not a certified environmental
controller. Add authentication, fail-safe hardware, alerting, watchdogs, and
equipment-specific limits before unattended or production use.
