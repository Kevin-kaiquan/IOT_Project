import csv
import threading, time, random, logging
from collections import deque
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

from sensors.scd41 import SCD41
from sensors import ds18b20
log = logging.getLogger("sampler")

class Sampler:
    def __init__(self, interval_sec: float = 3.0):
        self.interval = interval_sec
        self.history = deque(maxlen=HISTORY_LIMIT)
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._loop, daemon=True)

        self._log_dir = Path(__file__).resolve().parent.parent / "history_data"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file_path: Optional[Path] = None
        self._log_file = None
        self._csv_writer = None
        self._session_start = time.time()

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
        self._finalize_log()

    def _read_scd41(self) -> Tuple[Optional[float], Optional[float], Optional[float], str]:
        if self.scd41:
            try:
                co2, t_air, rh_air = self.scd41.read_cached(min_interval_sec=SCD41_MIN_INTERVAL)
                self._last_env = (co2, t_air, rh_air)
                return round(co2, 1), round(t_air, 2), round(rh_air, 1), "scd41"
            except Exception as e:
                log.warning(f"SCD41 read exception -> mock: {e}")
        last_co2, last_t, last_rh = self._last_env
        self._co2_mock = max(400.0, min(2000.0, self._co2_mock + random.uniform(-15, 15)))
        return (
            round(self._co2_mock, 1),
            None if last_t is None else round(last_t, 2),
            None if last_rh is None else round(last_rh, 1),
            "mock",
        )

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
                lux = self.veml7700.read_light()
                return round(lux, 2)
            except Exception as e:
                log.warning(f"VEML7700 read failed: {e}")
        return None

    def _ensure_log(self) -> None:
        if self._csv_writer is not None:
            return
        start_stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(self._session_start))
        self._log_file_path = self._log_dir / f"session_{start_stamp}_active.csv"
        self._log_file = self._log_file_path.open("w", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._log_file)
        self._csv_writer.writerow([
            "timestamp",
            "co2_ppm",
            "co2_source",
            "air_temp_c",
            "air_humidity",
            "probe_temp1_c",
            "probe_temp2_c",
            "light_lux",
        ])

    def _append_log_row(self, row: dict) -> None:
        self._ensure_log()
        if not self._csv_writer or not self._log_file:
            return
        self._csv_writer.writerow([
            row.get("ts"),
            row.get("co2_ppm"),
            row.get("co2_from"),
            row.get("t_air_c"),
            row.get("rh_air"),
            row.get("temp1_c"),
            row.get("temp2_c"),
            row.get("light"),
        ])
        self._log_file.flush()

    def _finalize_log(self) -> None:
        if self._log_file is None or self._log_file_path is None:
            return
        try:
            self._log_file.flush()
            self._log_file.close()
        except Exception:
            pass
        end_stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        final_path = self._log_dir / f"session_{end_stamp}.csv"
        try:
            self._log_file_path.rename(final_path)
        except Exception:
            final_path = self._log_dir / f"session_{end_stamp}_data.csv"
            try:
                self._log_file_path.rename(final_path)
            except Exception:
                pass
        self._log_file = None
        self._csv_writer = None
        self._log_file_path = None

    def _loop(self):
        while not self._stop.is_set():
            from datetime import datetime

            co2, t_air, rh_air, co2_src = self._read_scd41()
            t1, t2 = self._read_probe_t()
            light_intensity = self._read_light()

            entry = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "co2_ppm": co2,
                "co2_from": co2_src,
                "t_air_c": t_air,
                "rh_air": rh_air,
                "temp1_c": None if t1 is None else round(t1, 2),
                "temp2_c": None if t2 is None else round(t2, 2),
                "light": light_intensity,
            }

            self.history.append(entry)
            self._append_log_row(entry)
            time.sleep(self.interval)

    def snapshot(self) -> Dict[str, Any]:
        if self.history:
            now = self.history[-1]
        else:
            now = {
                "ts": "--",
                "co2_ppm": None,
                "co2_from": "--",
                "t_air_c": None,
                "rh_air": None,
                "temp1_c": None,
                "temp2_c": None,
                "light": None,
            }
        return {"now": now, "history": list(self.history)}
