"""Continuously print the default 1-Wire temperature sensor reading."""

import time
from w1thermsensor import W1ThermSensor

sensor = W1ThermSensor()

while True:
    temperature_in_celsius = sensor.get_temperature()
    print(f"Temperature: {temperature_in_celsius} °C")
    time.sleep(1)
