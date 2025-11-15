import RPi.GPIO as GPIO
import time

# 设置GPIO模式为BCM
GPIO.setmode(GPIO.BCM)

# 定义继电器控制的GPIO引脚
RELAY_PIN1 = 27  # GPIO27
RELAY_PIN2 = 22  # GPIO22
RELAY_PIN3 = 23  # GPIO23
RELAY_PIN4 = 24  # GPIO24

# 设置继电器引脚为输出
GPIO.setup(RELAY_PIN1, GPIO.OUT)
GPIO.setup(RELAY_PIN2, GPIO.OUT)
GPIO.setup(RELAY_PIN3, GPIO.OUT)
GPIO.setup(RELAY_PIN4, GPIO.OUT)

# 打开继电器（接通电源）
def turn_on_relay(pin):
    GPIO.output(pin, GPIO.HIGH)  # 继电器吸合，设备开始工作
    print(f"继电器 {pin} 已开启")

# 关闭继电器（断开电源）
def turn_off_relay(pin):
    GPIO.output(pin, GPIO.LOW)   # 继电器断开，设备停止工作
    print(f"继电器 {pin} 已关闭")

# 运行示例
try:
    turn_on_relay(RELAY_PIN1)  # 打开设备1
    time.sleep(2)    # 设备1运行5秒
    turn_off_relay(RELAY_PIN1) # 关闭设备1
    time.sleep(2)
    turn_off_relay(RELAY_PIN2)  # 打开设备2
    time.sleep(2)    # 设备2运行5秒
    turn_on_relay(RELAY_PIN2) # 关闭设备2
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
