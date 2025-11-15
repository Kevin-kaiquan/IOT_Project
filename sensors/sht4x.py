import time
from typing import Optional, Iterable, Tuple

try:
    from smbus2 import SMBus, i2c_msg
except Exception:
    SMBus = None
    i2c_msg = None

def _crc8(two: bytes) -> int:
    c = 0xFF
    for b in two:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x31) & 0xFF if (c & 0x80) else ((c << 1) & 0xFF)
    return c

class SHT4x:
    """
    简易稳态驱动：
    - 自动探测地址（默认 0x44 / 0x45）
    - read_cached(): 高重复度单次测量 (0xFD)，失败返回上次有效值
    """
    def __init__(self, busno: int = 1, addr: Optional[int] = None, addr_candidates: Iterable[int]=(0x44,0x45)):
        self.busno = busno
        self.addr = addr
        self.cands = list(addr_candidates)
        self._last: Optional[Tuple[float, float, float]] = None  # ts, tC, rh
        self._last_ts = 0.0

    def _ensure_addr(self):
        if self.addr is not None: return
        if SMBus is None: raise RuntimeError("smbus2 not available")
        for a in self.cands:
            try:
                # 发一次命令看是否 ACK
                with SMBus(self.busno) as bus:
                    bus.i2c_rdwr(i2c_msg.write(a, bytes([0xFD])))  # high repeatability, no clock-stretching
                self.addr = a
                return
            except Exception:
                continue
        raise RuntimeError(f"SHT4x not found on candidates {self.cands}")

    def _read_once(self):
        # 触发测量
        with SMBus(self.busno) as bus:
            bus.i2c_rdwr(i2c_msg.write(self.addr, bytes([0xFD])))
        time.sleep(0.01)  # datasheet: typ 9ms
        # 读 6 字节：T msb/lsb/CRC + RH msb/lsb/CRC
        with SMBus(self.busno) as bus:
            r = i2c_msg.read(self.addr, 6)
            bus.i2c_rdwr(r)
            b = bytes(list(r))
        if len(b) != 6: raise RuntimeError("SHT4x bad length")
        if _crc8(b[0:2]) != b[2] or _crc8(b[3:5]) != b[5]: raise RuntimeError("SHT4x CRC")
        t_raw = (b[0] << 8) | b[1]
        rh_raw = (b[3] << 8) | b[4]
        t_c = -45.0 + 175.0 * (t_raw / 65535.0)
        rh  = 100.0 * (rh_raw / 65535.0)
        return t_c, rh

    def read_cached(self, min_interval_sec: float = 2.0):
        self._ensure_addr()
        now = time.time()
        if self._last and (now - self._last_ts) < min_interval_sec:
            _, t, rh = self._last
            return t, rh
        try:
            t, rh = self._read_once()
            self._last = (now, t, rh)
            self._last_ts = now
            return t, rh
        except Exception:
            if self._last:
                _, t, rh = self._last
                return t, rh
            raise
