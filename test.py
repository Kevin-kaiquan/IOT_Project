import RPi.GPIO as GPIO
import time

GPIO.setmode(GPIO.BCM)

RELAY_PIN1 = 27
RELAY_PIN2 = 22
RELAY_PIN3 = 23
RELAY_PIN4 = 24

GPIO.setup(RELAY_PIN1, GPIO.OUT)
GPIO.setup(RELAY_PIN2, GPIO.OUT)
GPIO.setup(RELAY_PIN3, GPIO.OUT)
GPIO.setup(RELAY_PIN4, GPIO.OUT)

def turn_on_relay(pin):
    GPIO.output(pin, GPIO.HIGH)
    print(f"继电器 {pin} 已开启")

def turn_off_relay(pin):
    GPIO.output(pin, GPIO.LOW)
    print(f"继电器 {pin} 已关闭")

try:
    turn_on_relay(RELAY_PIN1)
    time.sleep(2)
    turn_off_relay(RELAY_PIN1)
    time.sleep(2)
    turn_off_relay(RELAY_PIN2)
    time.sleep(2)
    turn_on_relay(RELAY_PIN2)
    time.sleep(2)
    turn_on_relay(RELAY_PIN3)
    time.sleep(2)
    turn_off_relay(RELAY_PIN3)
    time.sleep(2)
    turn_on_relay(RELAY_PIN4)
    time.sleep(2)
    turn_off_relay(RELAY_PIN4)
finally:
    GPIO.cleanup()
