# IoT 菇房環境控制器

<p align="center">
  <a href="./README.md"><img alt="English" src="https://img.shields.io/badge/Language-English-2563eb"></a>
  <a href="./README.zh-TW.md"><img alt="繁體中文" src="https://img.shields.io/badge/語言-繁體中文-16a34a"></a>
</p>

這是一套以 Raspberry Pi 為核心的實驗性菇房環境監控與控制系統。專案整合溫度、
濕度、CO₂、光照感測器、繼電器設備、本機 Teachable Machine 影像分類、OLED、
CSV 歷史紀錄與瀏覽器儀表板。

> [!IMPORTANT]
> 本儲存庫**不包含對應的 3D 模型或外殼檔案**。如有需要，請直接聯絡
> [作者 Kevin-kaiquan](https://github.com/Kevin-kaiquan) 索取或詢問。

## 主要功能

- 讀取 SCD41 的 CO₂、空氣溫度與相對濕度。
- 讀取最多兩支 DS18B20 溫度探頭及一顆 VEML7700 光照感測器。
- 透過 GPIO 繼電器控制加熱器、風扇、LED 與霧化器。
- 依溫度、濕度及生長階段調整 CO₂ 控制策略。
- 從儀表板或 HTTP API 進行預設五分鐘的手動覆寫。
- 從兩台 USB 相機擷取畫面，並在本機執行 TFLite 分類。
- 提供即時數值、趨勢圖、相機及辨識狀態。
- 在 SSD1306／SH1106 OLED 上輪播感測資料。
- 將每次執行的感測紀錄寫入 `history_data/*.csv`。
- 部分硬體缺席時使用模擬資料，方便開發與除錯。

## 系統流程

```text
SCD41 ─┐
DS18B20├─> 取樣服務 ─> Flask API ─> 瀏覽器儀表板
VEML7700┘       │               └─> OLED
                └─> CSV 歷史紀錄

USB 相機 ─> OpenCV ─> TFLite 分類 ─> 生長階段
                                          │
感測值 + 生長階段 ─> 控制規則 ─> GPIO 繼電器
```

## 預設硬體配置

專案使用 BCM GPIO 編號。

| 硬體 | 介面／預設值 |
| --- | --- |
| 加熱器繼電器 | GPIO 27 |
| 風扇繼電器 | GPIO 22 |
| 相機 LED 繼電器 | GPIO 23 |
| 霧化器繼電器 | GPIO 17，低電位有效 |
| SCD41 | I²C bus 1，位址 `0x62` 或 `0x64` |
| VEML7700 | I²C bus 1，位址 `0x10` |
| OLED | I²C bus 1，位址 `0x3C` |
| DS18B20 | Linux 1-Wire，裝置名稱以 `28-` 開頭 |
| 相機 | USB 相機索引 0、1 |

> [!WARNING]
> 繼電器可能連接市電設備。請使用具有足夠額定值與隔離能力的模組，市電配線應由
> 合格人員完成；接上負載前，務必先確認高／低電位有效邏輯。

## 軟體需求

- Raspberry Pi OS Bookworm（建議 64 位元）
- Python 3.11
- 已在 `raspi-config` 啟用 I²C 與 1-Wire
- 如需影像辨識，需準備 Teachable Machine TFLite 模型
- 儀表板預設從 CDN 載入 Chart.js，因此需要網路；也可自行改成本機檔案

## 安裝

```bash
git clone https://github.com/Kevin-kaiquan/IOT_Project.git
cd IOT_Project

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

啟用硬體介面後通常需要重新啟動。執行帳號也必須具有 GPIO、I²C、1-Wire 與
視訊裝置的存取權限。

## 影像分類模型

建立 `model/` 目錄，放入 Teachable Machine 匯出的檔案：

```text
model/
├── model.tflite
└── labels.txt
```

預期標籤包括 `shiitake`（也接受常見拼法 `shitake`）、`base`、`mold` 及
`fly agaric`。模型缺少時應用程式仍可啟動，但不會執行分類。詳見
[`model/README.md`](model/README.md)。

## 設定

接上負載前請先檢查 [`config.py`](config.py)，尤其是：

- GPIO 腳位與霧化器有效電位
- 感測與相機辨識間隔
- I²C 位址
- CO₂ 目標及停止門檻
- 記憶體歷史筆數與 CSV 目錄
- 是否啟用 OLED

目前 `app.py` 內的生長階段規則如下：

| 階段 | 辨識條件 | CO₂ 行為 |
| --- | --- | --- |
| 未知 | 尚無穩定標籤 | 目標約 700 ppm |
| 菌絲期 | `base` | 目標約 900 ppm |
| 出菇期 | 連續辨識冬菇少於五次 | 目標約 750 ppm |
| 採收期 | 連續辨識冬菇至少五次 | 通風至約 500 ppm |

以上只是本專案預設值，並非通用栽培建議；請依品種、空間及設備重新驗證。

## 執行

```bash
python app.py
```

從同一網路的裝置開啟 `http://<樹莓派-IP>:5000`。伺服器會監聽所有網路介面，
而控制 API 沒有驗證機制，因此不要把 5000 連接埠直接暴露到公網。

感測資料會寫入 `history_data/`。執行中的檔名以 `_active.csv` 結尾；正常停止
後會重新命名為 `_complete.csv`。

若只想開發儀表板或 API，不啟動控制及相機背景工作：

```bash
IOT_DISABLE_BACKGROUND=1 flask --app app run
```

## HTTP API

| 方法 | 路徑 | 用途 |
| --- | --- | --- |
| `GET` | `/api/data` | 最新感測值、記憶體歷史、設備、覆寫及辨識狀態 |
| `GET` | `/api/camera/status` | 相機是否可用 |
| `GET` | `/api/camera/<id>/frame` | 最新 JPEG 畫面 |
| `GET` | `/api/control` | 設備與手動覆寫狀態 |
| `POST` | `/api/control` | 設定暫時手動覆寫 |
| `GET` / `POST` | `/api/atomizer` | 直接切換霧化器 |
| `GET` | `/api/oled/text?text=Hello&sec=2` | 暫時顯示 OLED 文字 |

範例：

```bash
curl -X POST http://raspberrypi.local:5000/api/control \
  -H "Content-Type: application/json" \
  -d '{"device":"fan","state":"on","duration_sec":300}'
```

完整請求與回應範例請參閱 [`docs/API.md`](docs/API.md)。

## 專案結構

```text
app.py                    Flask 路由與控制協調
config.py                 硬體及執行預設值
actuators/                GPIO 輸出驅動
sensors/                  SCD41、VEML7700、DS18B20 驅動
services/                 相機、TFLite、取樣及 OLED 服務
templates/index.html      瀏覽器儀表板
scripts/                  硬體自我測試工具
docs/API.md               HTTP API 文件
model/README.md           本機分類模型設定
```

## 硬體自我測試

只執行與已連接硬體相符的測試：

```bash
python scripts/relay_selftest.py
python scripts/temperature_selftest.py
python scripts/oled_selftest.py
```

繼電器測試會實際改變輸出狀態，執行前請閱讀
[`scripts/README.md`](scripts/README.md)。

## 常見問題

- **找不到 I²C 裝置：** 執行 `i2cdetect -y 1`，並檢查配線及 I²C 設定。
- **找不到 DS18B20：** 確認已啟用 1-Wire，並檢查
  `/sys/bus/w1/devices/28-*`。
- **相機沒有畫面：** 檢查 `ls /dev/video*`、USB 供電及裝置權限。
- **沒有分類結果：** 確認模型及標籤檔存在，並安裝與 Python 版本相容的
  TFLite runtime。
- **OLED 不可用：** 確認位址 `0x3C`，或在 `config.py` 將
  `OLED_ENABLE = False`。
- **CO₂ 顯示模擬值：** SCD41 無法讀取時會使用模擬資料，請從應用程式日誌
  查看實際錯誤。

## 專案狀態

本專案屬於原型與教學用途，不是經認證的環境控制器。無人值守或正式使用前，請
補上驗證、故障安全硬體、警報、看門狗與符合設備規格的限制。
