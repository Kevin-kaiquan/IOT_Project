import os, glob, time
from typing import Optional, List

if os.name == "posix":
    os.system("modprobe w1-gpio >/dev/null 2>&1")
    os.system("modprobe w1-therm >/dev/null 2>&1")
W1_BASE = "/sys/bus/w1/devices/"

def list_devices() -> List[str]:
    return sorted(glob.glob(os.path.join(W1_BASE, "28-*")))

def read_tempC(dev_dir: str, retries=3) -> Optional[float]:
    path = os.path.join(dev_dir, "w1_slave")
    for _ in range(retries):
        try:
            with open(path, "r") as f:
                l1, l2 = f.readline(), f.readline()
            if "YES" in l1:
                p = l2.strip().split("t=")
                if len(p)==2:
                    return float(p[1]) / 1000.0
        except Exception:
            pass
        time.sleep(0.05)
    return None
