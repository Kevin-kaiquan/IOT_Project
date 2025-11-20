# ------- Atomizer -------
ATOMIZER_PIN = 17
ATOMIZER_ACTIVE_HIGH = False     # 你的雾化器是低电平触发

# ------- Sampling / history -------
SAMPLE_INTERVAL_SEC = 3
HISTORY_LIMIT = 240

# ------- SCD41 (CO2 + Air T/RH) -------
SCD41_I2C_BUS = 1
SCD41_ADDRS = [0x62, 0x64]       # 你的板子是 0x62
SCD41_MIN_INTERVAL = 5.0         # 读取最小间隔（秒）

OLED_ENABLE   = True

VEML7700_I2C_BUS = 1
VEML7700_I2C_ADDR = 0x10

# config.py

# 设备引脚设置（继电器控制的GPIO引脚）
HEATER_PIN = 27      # 加热器控制引脚
FAN_PIN = 22         # 风扇控制引脚
LED_PIN = 23         # 继电器控制的补光 LED（仅在相机识别时点亮）

# ------- Camera LED / detection cadence -------
CAMERA_LED_WARMUP_SEC = 0.8
CAMERA_DETECT_MIN_SEC = 5.0
CAMERA_DETECT_MAX_SEC = 10.0

# ------- CO2 targets for growth ventilation -------
CO2_SAFE_TARGET = 700.0   # ppm，PID 目标
CO2_SAFE_STOP   = 650.0   # ppm，降到此值自动停风扇

# PID 控制参数
TEMP_KP = 1.0  # 温度控制的比例系数
TEMP_KI = 0.1  # 温度控制的积分系数
TEMP_KD = 0.05 # 温度控制的微分系数

HUMID_KP = 1.0  # 湿度控制的比例系数
HUMID_KI = 0.1  # 湿度控制的积分系数
HUMID_KD = 0.05 # 湿度控制的微分系数

# 设备控制的接受区（温度、湿度等的范围）
TEMP_ACCEPT_RANGE = 2  # 温度误差范围，例如 ±2°C
HUMID_ACCEPT_RANGE = 5 # 湿度误差范围，例如 ±5%
