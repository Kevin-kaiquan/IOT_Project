# sensors/EnvironmentController.py
"""
EnvironmentController / DeviceController

本模块只负责根据**已经采到的数据**控制三样东西：
- 加热器（通过继电器，HEATER_PIN）
- 风扇（FAN_PIN）
- 视觉用的 LED（LED_PIN，经继电器，由上层主动点亮)

传感器数据从 services.sampler.Sampler 那边来，由 app.py 周期性调用
DeviceController.update_environment(...) 完成控制。

逻辑（按照你现在的需求）：
1. 加热器 heater
   - 由两个探针温度 temp1 / temp2 决定
   - 使用它们的平均值 T_avg：
       如果 T_avg < TEMP_SET - TEMP_TOL  -> 打开加热器
       如果 T_avg > TEMP_SET + TEMP_TOL  -> 关闭加热器
   - 如果某一个温度 None，就用另一个；都 None 就跳过，不动继电器

2. 风扇 fan
   - 默认使用 CO₂ 浓度 co2_ppm 的滞回控制（CO2_HIGH / CO2_LOW）
   - 当识别到目标蘑菇时，上层会用 PID 方式请求它工作，保持 CO₂ 在安全范围内

3. LED
   - 仅由摄像流程主动点亮/熄灭，不再参与环境光照的自动补光
"""
from __future__ import annotations

import time
from typing import Optional

try:
    import RPi.GPIO as GPIO  # type: ignore
except Exception:  # 在非树莓派环境下做个 Mock，方便调试
    class _MockGPIO:
        BCM = BOARD = OUT = IN = LOW = HIGH = 0
        def setwarnings(self, *a, **k): pass
        def setmode(self, *a, **k): pass
        def setup(self, *a, **k): pass
        def output(self, *a, **k): pass
        def cleanup(self, *a, **k): pass
    GPIO = _MockGPIO()  # type: ignore

from config import HEATER_PIN, FAN_PIN, LED_PIN


class DeviceController:
    def __init__(
        self,
        heater_pin: int = HEATER_PIN,
        fan_pin: int = FAN_PIN,
        led_pin: int = LED_PIN,
    ) -> None:
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)

        self.heater_pin = heater_pin
        self.fan_pin = fan_pin
        self.led_pin = led_pin

        GPIO.setup(self.heater_pin, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(self.fan_pin, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(self.led_pin, GPIO.OUT, initial=GPIO.LOW)

        self.heater_on = False
        self.fan_on = False
        self.led_on = False

        # 记录上一次改变状态的时间，后面如果想加最小间隔限制可以用到
        self._last_change = {
            "heater": 0.0,
            "fan": 0.0,
            "led": 0.0,
        }

    # ---------------- 低层封装：真正去拉 GPIO ----------------
    def _set_pin(self, name: str, pin: int, on: bool) -> None:
        now = time.time()
        self._last_change[name] = now
        GPIO.output(pin, GPIO.HIGH if on else GPIO.LOW)

    def _set_heater(self, on: bool) -> None:
        if on != self.heater_on:
            self.heater_on = on
            self._set_pin("heater", self.heater_pin, on)

    def _set_fan(self, on: bool) -> None:
        if on != self.fan_on:
            self.fan_on = on
            self._set_pin("fan", self.fan_pin, on)

    def _set_led(self, on: bool) -> None:
        if on != self.led_on:
            self.led_on = on
            self._set_pin("led", self.led_pin, on)

    # ---------------- 直接控制：提供给外部强制拉高/拉低 ----------------
    def set_heater(self, on: bool) -> None:
        self._set_heater(bool(on))

    def set_fan(self, on: bool) -> None:
        self._set_fan(bool(on))

    def set_led(self, on: bool) -> None:
        self._set_led(bool(on))

    # ---------------- 高层逻辑：根据数据做决策 ----------------
    def update_environment(
        self,
        temp1: Optional[float],
        temp2: Optional[float],
        co2_ppm: Optional[float],
        *,
        temp_set: float = 22.0,
        temp_tolerance: float = 0.5,
        co2_high: float = 1000.0,
        co2_low: float = 800.0,
    ) -> None:
        """
        主入口：由 app.py 周期性调用。

        参数：
          temp1, temp2 : 两个温度探针（单位 °C）
          co2_ppm      : CO₂ 浓度（ppm）
        """

        # ---- 1) 加热器：根据两个温度传感器 ----
        avg_temp: Optional[float] = None
        if temp1 is not None and temp2 is not None:
            avg_temp = 0.5 * (float(temp1) + float(temp2))
        elif temp1 is not None:
            avg_temp = float(temp1)
        elif temp2 is not None:
            avg_temp = float(temp2)

        if avg_temp is not None:
            if avg_temp < temp_set - temp_tolerance:
                # 温度偏低 → 打开加热器
                self._set_heater(True)
            elif avg_temp > temp_set + temp_tolerance:
                # 温度明显高于设定 → 关闭加热器
                self._set_heater(False)
            # 否则落在中间“死区”，保持原状态不变
        # 如果完全读不到温度，就保持 heater 当前状态，不瞎动

        # ---- 2) 风扇：只看 CO₂ ----
        if co2_ppm is not None:
            co2_val = float(co2_ppm)
            if co2_val > co2_high:
                # CO2 很高 → 打开风扇排风
                self._set_fan(True)
            elif co2_val < co2_low:
                # CO2 已经降下来 → 关闭风扇
                self._set_fan(False)
            # 中间区域同样保持原状态

    # ---------------- 供 API 查询当前状态 ----------------
    @property
    def states(self) -> dict:
        return {
            "heater": "on" if self.heater_on else "off",
            "fan": "on" if self.fan_on else "off",
            "led": "on" if self.led_on else "off",
        }

    def cleanup(self) -> None:
        try:
            GPIO.cleanup()
        except Exception:
            pass


# 命令行简单自测（连接好硬件再用）
if __name__ == "__main__":
    import time
    dc = DeviceController()
    try:
        for i in range(10):
            t1 = 20 + i * 0.3
            t2 = 20 + i * 0.2
            co2 = 600 + i * 80
            light = 30 + i * 5
            print(f"[demo] t1={t1:.1f} t2={t2:.1f} co2={co2:.0f} light={light:.0f}")
            dc.update_environment(t1, t2, co2, light)
            time.sleep(1.0)
    finally:
        dc.cleanup()
