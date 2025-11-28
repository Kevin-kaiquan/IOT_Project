import time
from smbus2 import SMBus

class VEML7700:

    VEML_ADDR = 0x10
    REG_ALS_CONF = 0x00
    REG_ALS = 0x04
    K = 0.0672

    def __init__(self, busno: int = 1, addr: int = VEML_ADDR):
        self.busno = busno
        self.addr = addr

    def _init_sensor(self):
        try:
            with SMBus(self.busno) as bus:
                conf_val = 0x0000
                bus.write_i2c_block_data(self.addr, self.REG_ALS_CONF, [conf_val & 0xFF, (conf_val >> 8) & 0xFF])
                time.sleep(0.01)
        except Exception as e:
            print(f"[VEML7700 Error] Initialization failed: {e}")
            raise

    def _read_raw(self) -> int:
        try:
            with SMBus(self.busno) as bus:
                data = bus.read_i2c_block_data(self.addr, self.REG_ALS, 2)
                return data[0] | (data[1] << 8)
        except Exception as e:
            print(f"[VEML7700 Error] Reading failed: {e}")
            return 0

    def _raw_to_lux(self, raw: int, gain: float = 1.0, it_ms: int = 100) -> float:
        return raw * self.K * (100.0 / float(it_ms)) / float(gain)

    def read_light(self) -> float:
        try:
            self._init_sensor()
            time.sleep(0.15)
            raw = self._read_raw()
            lux = self._raw_to_lux(raw, gain=1.0, it_ms=100)
            return round(lux, 2)
        except Exception as e:
            print(f"[VEML7700 Error] {e}")
            return None
