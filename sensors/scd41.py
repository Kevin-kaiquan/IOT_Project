import time, logging
from typing import Optional, Tuple, List, Iterable

try:
    from smbus2 import SMBus, i2c_msg
except Exception:
    SMBus = None; i2c_msg = None

log = logging.getLogger("scd41")

def _crc8(two: bytes) -> int:
    c=0xFF
    for b in two:
        c^=b
        for _ in range(8):
            c=((c<<1)^0x31)&0xFF if (c&0x80) else ((c<<1)&0xFF)
    return c

class SCD41:
    """
    稳态驱动：
    - 地址探测用 get_data_ready_status（任何状态都可读）
    - start(): stop→reinit→start，统一设备状态
    - read_cached(): 失败优先返回缓存；连续 I/O 错误自动软复位
    """
    def __init__(self, busno=1, addr: Optional[int]=None, addr_candidates: Iterable[int]=(0x62,0x64)):
        self.busno=busno
        self.addr=addr
        self.cands=list(addr_candidates)
        self.started=False
        self._last: Optional[Tuple[float,float,float,float]] = None  # ts,co2,tC,rh
        self._last_ts=0.0
        self._err_streak=0

    # ---- low level ----
    def _w(self, cmd:int, args:List[int]=None):
        if self.addr is None: raise RuntimeError("addr not set")
        data=[(cmd>>8)&0xFF, cmd&0xFF]
        if args:
            for w in args:
                hi,lo=(w>>8)&0xFF, w&0xFF
                data += [hi,lo,_crc8(bytes([hi,lo]))]
        with SMBus(self.busno) as bus:
            bus.i2c_rdwr(i2c_msg.write(self.addr, bytes(data)))

    def _r(self, cmd:int, n:int, delay_ms:int=2)->bytes:
        if self.addr is None: raise RuntimeError("addr not set")
        with SMBus(self.busno) as bus:
            bus.i2c_rdwr(i2c_msg.write(self.addr, bytes([(cmd>>8)&0xFF, cmd&0xFF])))
            if delay_ms: time.sleep(delay_ms/1000.0)
            rd=i2c_msg.read(self.addr, n); bus.i2c_rdwr(rd); return bytes(list(rd))

    # ---- helpers ----
    def _probe_ready(self, a:int)->bool:
        try:
            with SMBus(self.busno) as bus:
                bus.i2c_rdwr(i2c_msg.write(a, bytes([0xE4,0xB8])))
                time.sleep(0.002)
                rd=i2c_msg.read(a,3); bus.i2c_rdwr(rd)
            b=bytes(list(rd))
            if len(b)!=3: return False
            msb,lsb,crc=b[0],b[1],b[2]
            return _crc8(bytes([msb,lsb]))==crc
        except Exception:
            return False

    def _ensure_addr(self):
        if self.addr is not None: return
        if SMBus is None: raise RuntimeError("smbus2 not available")
        for a in self.cands:
            if self._probe_ready(a):
                self.addr=a
                log.info(f"SCD41 detected at 0x{a:02X} on i2c-{self.busno}")
                return
        raise RuntimeError(f"SCD41 not found in {self.cands}")

    # ---- control ----
    def start(self):
        self._ensure_addr()
        # 统一状态：停测→复位→启动
        try:
            self._w(0x3F86); time.sleep(1.0)  # stop
        except Exception:
            pass
        self._w(0x3646); time.sleep(0.02)     # reinit
        self._w(0x21B1)                       # start periodic
        self.started=True
        self._last_ts=0.0
        self._err_streak=0

    # ---- reading ----
    def _ready(self)->bool:
        b=self._r(0xE4B8,3)
        msb,lsb,crc=b[0],b[1],b[2]
        if _crc8(bytes([msb,lsb]))!=crc: return False
        return ((msb<<8)|lsb)!=0

    def _read_once(self):
        b=self._r(0xEC05,9)
        def u16(i):
            msb,lsb,crc=b[i],b[i+1],b[i+2]
            if _crc8(bytes([msb,lsb]))!=crc: raise RuntimeError("CRC")
            return (msb<<8)|lsb
        co2=float(u16(0))
        tC=-45.0 + 175.0*(u16(3)/65535.0)
        rh=100.0*(u16(6)/65535.0)
        return co2,tC,rh

    def read_cached(self, min_interval_sec: float = 5.0):
        self._ensure_addr()
        if not self.started:
            self.start()
            time.sleep(5.2)  # 等首帧

        now=time.time()
        if self._last and (now-self._last_ts)<min_interval_sec:
            _,co2,tC,rh=self._last; return co2,tC,rh

        try:
            if not self._ready():
                if self._last:
                    _,co2,tC,rh=self._last; return co2,tC,rh
                time.sleep(1.0)
                if not self._ready(): raise RuntimeError("not ready")
            co2,tC,rh=self._read_once()
            self._last=(now,co2,tC,rh); self._last_ts=now
            self._err_streak=0
            return co2,tC,rh

        except OSError as e:
            # Errno 5/121: I/O 错误/远端无应答，做软复位自愈
            if getattr(e, "errno", None) in (5, 121):
                self._err_streak += 1
                log.warning(f"SCD41 read failed: {e} (streak={self._err_streak})")
                if self._err_streak >= 3:
                    log.warning("SCD41 soft reset (stop→reinit→start)")
                    try:
                        self.start()
                    except Exception as ee:
                        log.error(f"SCD41 restart failed: {ee}")
                    time.sleep(0.1)
            else:
                log.warning(f"SCD41 read failed: {e}")

            if self._last:
                _,co2,tC,rh=self._last; return co2,tC,rh
            raise

        except Exception as e:
            log.warning(f"SCD41 read failed: {e}")
            if self._last:
                _,co2,tC,rh=self._last; return co2,tC,rh
            raise

    def last(self):
        if self._last:
            _,co2,tC,rh=self._last; return co2,tC,rh
        return None
