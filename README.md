# IoT Mushroom Environment Controller

<p align="center">
  <a href="./README.md"><img alt="English" src="https://img.shields.io/badge/Language-English-2563eb"></a>
  <a href="./README.zh-TW.md"><img alt="繁體中文" src="https://img.shields.io/badge/語言-繁體中文-16a34a"></a>
</p>

An experimental Raspberry Pi system for monitoring and controlling a mushroom
growing environment. It combines temperature, humidity, CO₂, and light sensors
with relay-controlled equipment, a local Teachable Machine image classifier,
an OLED display, CSV history logging, and a browser dashboard.

> [!IMPORTANT]
> The corresponding **3D model / enclosure files are not included** in this
> repository. If you need them, please contact the
> [author, Kevin-kaiquan](https://github.com/Kevin-kaiquan), directly.

## Features

- Reads SCD41 CO₂, air temperature, and relative humidity.
- Reads up to two DS18B20 temperature probes and one VEML7700 light sensor.
- Controls a heater, fan, LED, and atomizer through GPIO-connected relays.
- Applies automatic temperature, humidity, and growth-phase-aware CO₂ rules.
- Supports five-minute manual overrides from the dashboard or HTTP API.
- Captures either of two USB cameras and runs local TFLite classification.
- Shows live values and charts in an English web dashboard.
- Displays a rotating sensor summary on an SSD1306/SH1106 OLED.
- Writes each runtime session to `history_data/*.csv`.
- Falls back to mock readings where practical, which helps development without
  all sensors attached.

## System overview

```text
SCD41 ─┐
DS18B20├─> Sampler ─> Flask API ─> Browser dashboard
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
