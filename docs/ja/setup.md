# I2C セットアップ

[English](../setup.md) | 日本語

[README](../../README.ja.md) · [Python・C API](api.md) · [UART サンプル](uart.md)

Raspberry Pi OS Trixie 以降と **Spresense GNSS Add-on Board**
（CXD5602PWBGNSS1W、CXD5610GF）を対象とした手順です。
Raspberry Pi Zero 2 W、I2C 400 kHz、GPIO17 の構成で動作を確認しています。

## 1. システムの依存パッケージをインストールする

```sh
sudo apt install build-essential python3-dev python3-venv libgpiod-dev gpiod
sudo usermod -aG i2c,gpio "$USER"
```

グループへの追加は、後述の再起動後に反映されます。Python 3.13 以上と
libgpiod 2.0 以上が必要です。Python のビルド依存パッケージは pip がインストールします。

## 2. ボードを接続する

**I2C SDA・I2C SCL・IRQIN** を、GNSS ボードの **1.8 V** と
Raspberry Pi の **3.3 V** の間でレベル変換して接続します。
回路例では、RT9193-18GB レギュレーター、LSF0108PWR レベル変換 IC、
4 ピンソケット 3 個を使用しています。

3 枚の接続図に共通して、`VCC` は Pi の **3.3 V** 電源（物理ピン 1 または 17）、
`VDD_1.8V` はレギュレーターの **1.8 V** 出力を表します。
同じ信号名の端子同士を接続し、Pi・レギュレーター・レベル変換 IC・GNSS ボードの GND を共通にしてください。

### 1.8 V 電源を生成する

RT9193-18GB（U3）の VIN（ピン 1）と EN（ピン 3）を 3.3 V に、GND（ピン 2）を GND に接続します。
VOUT（ピン 5）から GNSS ボードと LSF0108 の基準電圧入力へ 1.8 V を供給します。
C9 は 1 µF、BP 端子のコンデンサー C11 は 22 nF です。

![RT9193-18GB による 1.8 V 電源。C10 の容量は下記の訂正を参照](../images/power-supply-1v8.png)

**C10 の訂正：** 図では 0.1 µF ですが、VOUT–GND 間には **1 µF 以上のセラミック出力コンデンサー**を
レギュレーターの近くに配置してください。容量と ESR は
[RT9193 データシートの Applications Information](https://www.richtek.com/assets/product_file/RT9193/DS9193-18.pdf#page=8)
に示された安定動作の条件を満たす必要があります。

### 3 本の信号をレベル変換する

動作確認した回路では [LSF0108PWR](https://www.ti.com/product/LSF0108/part-details/LSF0108PWR)（U2）を使用しています。
A 側を GNSS ボード、B 側を Raspberry Pi に接続します。

![LSF0108PWR による SDA・SCL・IRQIN のレベル変換。信号のプルアップは下記を参照](../images/level-shifter-1v8.png)

| 信号 | A 側：GNSS ボード | B 側：Raspberry Pi |
| --- | --- | --- |
| SDA | A1、ピン 3：`I2C_SDA_1.8V` | B1、ピン 18：`I2C_SDA` → GPIO2 |
| SCL | A2、ピン 4：`I2C_SCL_1.8V` | B2、ピン 17：`I2C_SCL` → GPIO3 |
| IRQIN | A3、ピン 5：`GPS_INT_GPIO17_1.8V` | B3、ピン 16：`GPS_INT_GPIO17` → GPIO17 |

Vref_A（ピン 2）を 1.8 V、GND（ピン 1）を共通 GND に接続します。
Vref_B（ピン 19）と EN（ピン 20）を接続し、その接続点を R1（200 kΩ）経由で 3.3 V につなぎます。
C5 と C6 は 0.1 µF です。A4–A8 と B4–B8 は未接続にします。

**図では信号線のプルアップ抵抗を省略しています。** B 側の各信号には 3.3 V へのプルアップが必要です。

- I2C の B 側：Pi Zero 2 W の GPIO2/GPIO3 に実装済みの 1.8 kΩ プルアップを使用します。
  [Pi の回路図](https://datasheets.raspberrypi.com/rpizero2/raspberry-pi-zero-2-w-reduced-schematics.pdf)を参照してください。
- I2C の A 側：GNSS ボードに実装済みの、1.8 V への 4.7 kΩ プルアップ（R6/R7）を使用します。
  [Sony の回路図](https://github.com/sonydevworld/spresense-hw-design-files/blob/master/CXD5602PWBGNSS1/schematics/CXD5602PWBGNSS1_schematics.pdf)を参照してください。
- IRQIN の B 側（`GPS_INT_GPIO17`）：3.3 V への外付けプルアップ抵抗を追加してください。
  ドライバーは GPIO17 の内部プルアップを有効にしません。

プルアップ抵抗は、バス容量・立ち上がり時間・ドライバーのシンク電流を考慮し、
[TI の Pull-Up Resistor Sizing](https://www.ti.com/lit/ds/symlink/lsf0108.pdf#page=15)に従って選定してください。
200 kΩ の R1 は Vref_B/EN のバイアス用であり、信号線のプルアップの代わりにはなりません。

### ソケットを接続する

回路例では、4 ピンソケット **F136-1104S3CMUB2** を 3 個使用します。
**この図の CN3/CN4/CN5 はソケットの名称**で、GNSS ボード上の CN1/CN2 とは別です。

![4 ピンソケット 3 個を使った GNSS Add-on Board の接続](../images/gnss-addon-sockets.png)

| ソケット | GNSS ボードのピン（ソケットのピン 1→4 順） | 同じ順序の信号 |
| --- | --- | --- |
| CN3 | CN1 のピン 1、2、3、4 | GND、未接続、未接続、未接続 |
| CN4 | CN1 のピン 10、11、12、13 | 3.3 V、1.8 V、IRQIN、未接続 |
| CN5 | CN2 のピン 10、11、12、13 | GND、I2C SCL、I2C SDA、未接続 |

Sony の[部品面から見たピン配置](https://developer.spresense.sony-semicon.com/products/gnss-add-on-board)では、
左が CN1、右が CN2、上端がピン 1 です。両コネクターとも 13 ピンあります。
この対応表と照合してソケットの向きを確認してください。図の緑の × 印は未接続を表します。

| 信号・電源 | GNSS ボードのピン | Pi の GPIO・電源 | Pi の物理ピン | 接続 |
| --- | --- | --- | --- | --- |
| SDA | CN2-12（D14） | GPIO2 / SDA1 | 3 | 3.3 V ↔ 1.8 V レベル変換 |
| SCL | CN2-11（D15） | GPIO3 / SCL1 | 5 | 3.3 V ↔ 1.8 V レベル変換 |
| IRQIN | CN1-12（D22） | GPIO17 入力 | 11 | ボードの 1.8 V 出力 → Pi の 3.3 V 入力 |
| GND | CN1-1 と CN2-10 | GND | 例：6 | 共通 GND |
| 1.8 V | CN1-11 | RT9193 出力、`VDD_1.8V` | — | ボード電源 |
| 3.3 V | CN1-10 | 3.3 V | 1 または 17 | アンテナ電源を含むボード電源 |

`IRQIN` は GNSS からホストへの通知信号に付けられたボード上の名称です。Pi 側は入力に設定します。
上記の GPIO 番号は BCM 番号であり、ヘッダーの物理ピン番号とは異なります。

## 3. I2C を 400 kHz で有効にする

クロックは Raspberry Pi OS が設定し、同じ I2C バス上のすべてのデバイスに適用されます。
起動設定ファイルをバックアップして開きます。

```sh
sudo cp -an /boot/firmware/config.txt /boot/firmware/config.txt.before-cxd56xx
sudo nano /boot/firmware/config.txt
```

`[pi5]` などの条件付きセクションや `dtoverlay` 指定より前の、ファイル冒頭付近に次の設定を記載します。
既存の項目がある場合は、重複して追加せずに編集してください。

```ini
dtparam=i2c_arm=on
dtparam=i2c_arm_baudrate=400000
```

再起動して接続し直し、デバイスツリーに記録された周波数を確認します。

```sh
sudo reboot
```

```sh
python3 - <<'PY'
from pathlib import Path

path = Path("/sys/bus/i2c/devices/i2c-1/of_node/clock-frequency")
print(int.from_bytes(path.read_bytes(), "big"), "Hz")
PY
```

`400000 Hz` と表示されれば設定が反映されています。別のバスを確認する場合は `i2c-1` を変更してください。
参考：[Raspberry Pi の DT パラメーター](https://www.raspberrypi.com/documentation/computers/configuration.html#dt-parameters)、
[NuttX の 400 kHz 設定](https://github.com/apache/nuttx/blob/87f134cfaa9e1474515deffb1e1224ab16f5b48e/boards/arm/cxd56xx/common/src/cxd56_gnss_addon.c#L46)、
[REYAX Software Guide §2.2](https://reyax.com/upload/products_download/download_file/RYS8839_Software_Guide.pdf#page=8)。

<a id="4-select-the-gpio-chip-and-install-from-pypi"></a>

## 4. GPIO チップを選択して PyPI からインストールする

| 設定 | デフォルト | ビルド時の指定 |
| --- | --- | --- |
| I2C デバイス | `/dev/i2c-1` | `CXD5610_I2C_DEV` マクロ |
| 7 ビット I2C アドレス | `0x24` | `CXD5610_I2C_ADDR` マクロ |
| GPIO チップ | `/dev/gpiochip4` | `CXD56XX_GPIO_CHIP` 環境変数 |
| 割り込みラインのオフセット | `17` | `CXD5610_INT_GPIO` マクロ |

チップの番号はボードや OS によって異なります。GPIO17 を含むチップと、そのラインオフセットを確認してください。

```sh
gpiodetect
gpioinfo
```

例えば `/dev/gpiochip0` の場合は、次のようにインストールします。

```sh
python3 -m venv .venv
. .venv/bin/activate
CXD56XX_GPIO_CHIP=/dev/gpiochip0 python -m pip install --no-cache-dir rpi-cxd56xx-gnss
cxd56xx-gnss
```

時刻・座標・測位状態・衛星数が表示されます。終了は **Ctrl+C** です。
測位するには、アンテナを空が見える場所に置いてください。

GPIO の選択はドライバーに組み込まれます。変更するにはソースから再ビルドしてください。

```sh
CXD56XX_GPIO_CHIP=/dev/gpiochip0 python -m pip install \
  --force-reinstall --no-cache-dir --no-binary=rpi-cxd56xx-gnss rpi-cxd56xx-gnss
```

OS の I2C クロックだけを変更する場合は、再ビルド不要です。
[cxd5610_rpi.h](../../src/cxd56xx_gnss/cxd5610_rpi.h) にあるその他のデフォルト値は、
コンパイラーの `-D` オプションで上書きできます。[リポジトリからのビルド](api.md#building-from-a-checkout)を参照してください。
