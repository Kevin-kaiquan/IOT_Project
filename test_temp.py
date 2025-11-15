import time
from w1thermsensor import W1ThermSensor

# 获取所有连接的传感器
sensor = W1ThermSensor()

# 读取温度并打印
while True:
    temperature_in_celsius = sensor.get_temperature()
    print(f"Temperature: {temperature_in_celsius}°C")
    time.sleep(1)  # 每2秒读取一次
