from __future__ import annotations

import time
from typing import Optional

try:
    import RPi.GPIO as GPIO  # type: ignore
except Exception:  # Provide GPIO mock when hardware is unavailable
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
    """Controls heater, fan, and LED outputs."""
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

        self._last_change = {
            "heater": 0.0,
            "fan": 0.0,
            "led": 0.0,
        }

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

    def set_heater(self, on: bool) -> None:
        self._set_heater(bool(on))

    def set_fan(self, on: bool) -> None:
        self._set_fan(bool(on))

    def set_led(self, on: bool) -> None:
        self._set_led(bool(on))

    def update_environment(
        self,
        temp1: Optional[float],
        temp2: Optional[float],
        co2_ppm: Optional[float],
        light: Optional[float],
        *,
        temp_set: float = 22.0,
        temp_tolerance: float = 0.5,
        co2_high: float = 1000.0,
        co2_low: float = 800.0,
        manage_fan: bool = True,
        manage_heater: bool = True,
    ) -> None:
        """Apply heater and fan rules based on sensor inputs."""
        if manage_heater:
            avg_temp: Optional[float] = None
            if temp1 is not None and temp2 is not None:
                avg_temp = 0.5 * (float(temp1) + float(temp2))
            elif temp1 is not None:
                avg_temp = float(temp1)
            elif temp2 is not None:
                avg_temp = float(temp2)

            if avg_temp is not None:
                if avg_temp < temp_set - temp_tolerance:
                    self._set_heater(True)
                elif avg_temp > temp_set + temp_tolerance:
                    self._set_heater(False)

        if manage_fan and co2_ppm is not None:
            co2_val = float(co2_ppm)
            if co2_val > co2_high:
                self._set_fan(True)
            elif co2_val < co2_low:
                self._set_fan(False)

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
