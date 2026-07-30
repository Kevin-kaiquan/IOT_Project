import csv
import logging
import random
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from sensors.veml7700 import VEML7700

try:
    import config as CFG
except Exception:
    class _CFG: pass
    CFG = _CFG()

HISTORY_LIMIT      = getattr(CFG, "HISTORY_LIMIT", 240)
SCD41_I2C_BUS      = getattr(CFG, "SCD41_I2C_BUS", 1)
SCD41_ADDRS        = getattr(CFG, "SCD41_ADDRS", [0x62, 0x64])
SCD41_MIN_INTERVAL = getattr(CFG, "SCD41_MIN_INTERVAL", 5.0)

VEML7700_I2C_ADDR = getattr(CFG, "VEML7700_I2C_ADDR", 0x10)
VEML7700_I2C_BUS = getattr(CFG, "VEML7700_I2C_BUS", 1)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
HISTORY_DATA_DIR = Path(getattr(CFG, "HISTORY_DATA_DIR", "history_data"))
if not HISTORY_DATA_DIR.is_absolute():
    HISTORY_DATA_DIR = PROJECT_ROOT / HISTORY_DATA_DIR

from sensors.scd41 import SCD41
from sensors import ds18b20
log = logging.getLogger("sampler")

class Sampler:
    def __init__(self, interval_sec: float = 3.0):
        self.interval = interval_sec
        self.history = deque(maxlen=HISTORY_LIMIT)
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._log_file = None
        self._log_path: Optional[Path] = None
        self._csv_writer = None

        self.veml7700: Optional[VEML7700] = None
        try:
            self.veml7700 = VEML7700(busno=VEML7700_I2C_BUS, addr=VEML7700_I2C_ADDR)
            log.info(f"VEML7700 initialized with address {VEML7700_I2C_ADDR}")
        except Exception as e:
            log.error(f"VEML7700 init failed: {e}")
            self.veml7700 = None

        self.scd41: Optional[SCD41] = None
        try:
            self.scd41 = SCD41(busno=SCD41_I2C_BUS, addr=None, addr_candidates=SCD41_ADDRS)
            self.scd41.start()
        except Exception as e:
            log.error(f"SCD41 init failed: {e}")
            self.scd41 = None

        self._co2_mock = 600.0
        self._last_env: Tuple[Optional[float], Optional[float], Optional[float]] = (None, None, None)

    def start(self):
        if not self._th.is_alive():
            self._th.start()

    def stop(self):
        self._stop.set()
        self._th.join(timeout=1.0)
        self._close_log()…9819 tokens truncated… Sampler ─> Flask API ─> Browser dashboard
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
