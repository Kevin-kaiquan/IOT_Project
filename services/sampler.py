import threading, time, random, logging
from collections import deque
from typing import Optional, Dict, Any, Tuple
from sensors.veml7700 import VEML7700  # Import the VEML7700 class

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

from sensors.scd41 import SCD41
from sensors import ds18b20
log = logging.getLogger("sampler")

class Sampler:
    def __init__(self, interval_sec: float = 3.0):
        self.interval = interval_sec
        self.history = deque(maxlen=HISTORY_LIMIT)
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._loop, daemon=True)

        # --- VEML7700 for Light Intensity ---
        self.veml7700: Optional[VEML7700] = None
        try:
            self.veml7700 = VEML7700(busno=VEML7700_I2C_BUS, addr=VEML7700_I2C_ADDR)
            log.info(f"VEML7700 initialized with address {VEML7700_I2C_ADDR}")
        except Exception as e:
            log.error(f"VEML7700 init failed: {e}")
            self.veml7700 = None

        # --- SCD41 for CO2 + ambient T/RH ---
        self.scd41: Optional[SCD41] = None
        try:
            self.scd41 = SCD41(busno=SCD41_I2C_BUS, addr=None, addr_candidates=SCD41_ADDRS)
            self.scd41.start()
        except Exception as e:
            log.error(f"SCD41 init failed: {e}")
            self.scd41 = None

        self._co2_mock = 600.0

    def start(self):
        if not self._th.is_alive():
            self._th.start()

    def stop(self):
        self._stop.set()
        self._th.join(timeout=1.0)

    def _read_co2_trh(self) -> Tuple[float, Optional[float], Optional[float], str]:
        if self.scd41:
            try:
                co2, t_air, rh_air = self.scd41.read_cached(min_interval_sec=SCD41_MIN_INTERVAL)
                return round(co2, 1), round(t_air, 2), round(rh_air, 1), "scd41"
            except Exception as e:
                log.warning(f"SCD41 read exception -> mock: {e}")
        # fallback mock
        self._co2_mock = max(400.0, min(2000.0, self._co2_mock + random.uniform(-15, 15)))
        base_t = 22.0 + random.uniform(-1.0, 1.0)
        rh = 60.0 + random.uniform(-5.0, 5.0)
        return round(self._co2_mock, 1), round(base_t, 2), round(rh, 1), "mock"

    def _read_probe_t(self) -> Tuple[Optional[float], Optional[float]]:
        devs = ds18b20.list_devices()
        t1 = ds18b20.read_tempC(devs[0]) if len(devs) >= 1 else None
        t2 = ds18b20.read_tempC(devs[1]) if len(devs) >= 2 else None
        if t1 is not None and t2 is None and len(devs) == 1: t2 = t1
        if t1 is None and t2 is None:
            base = 22 + 2 * (random.random() - 0.5)
            t1 = base
            t2 = base + (random.random() - 0.5)
        return t1, t2

    def _read_light(self) -> Optional[float]:
        """Reads light intensity from VEML7700 sensor"""
        if self.veml7700:
            try:
                lux = self.veml7700.read_light()  # Get light intensity from VEML7700
                return round(lux, 2)
            except Exception as e:
                log.warning(f"VEML7700 read failed: {e}")
        return None

    def _loop(self):
        from datetime import datetime
        while not self._stop.is_set():
            co2, t_air, rh_air, co2_src = self._read_co2_trh()
            t1, t2 = self._read_probe_t()
            light_intensity = self._read_light()  # Get light intensity from VEML7700

            self.history.append({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "co2_ppm": co2,
                "co2_from": co2_src,
                "t_air_c": t_air,
                "rh_air": rh_air,
                "temp1_c": None if t1 is None else round(t1, 2),
                "temp2_c": None if t2 is None else round(t2, 2),
                "light": light_intensity  # Add light intensity to history
            })
            time.sleep(self.interval)

    def snapshot(self) -> Dict[str, Any]:
        if self.history:
            now = self.history[-1]
        else:
            now = {"ts": "--", "co2_ppm": None, "co2_from": "--", "t_air_c": None, "rh_air": None, "temp1_c": None, "temp2_c": None, "light": None}
        return {"now": now, "history": list(self.history)}
