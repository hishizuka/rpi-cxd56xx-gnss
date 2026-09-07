# Python・C API

[English](../api.md) | 日本語

[README](../../README.ja.md) · [セットアップ](setup.md) · [UART サンプル](uart.md)

## Python

[PyPI からインストール](setup.md#4-select-the-gpio-chip-and-install-from-pypi)した後、`CXD56xx` をインポートします。
コンストラクターはバックグラウンド処理を起動し、測位の停止、衛星選択とバイナリー通知の設定、測位の再開を行います。
Python の呼び出し元から起動コマンドを送る必要はありません。

```python
import errno
import time

from cxd56xx_gnss import CXD56xx

gps = CXD56xx()
try:
    while True:
        result = gps.peek()
        if result == 0:
            print(gps.lat, gps.lon, gps.alt, gps.speed, gps.mode)
        elif result != -errno.EAGAIN:
            raise OSError(-result, "GNSS read failed")
        time.sleep(1)
finally:
    del gps
```

実行用のサンプルは [examples/i2c_read.py](../../examples/i2c_read.py) にあります。
ダウンロードするか、リポジトリを取得し、仮想環境を有効にした状態で `python examples/i2c_read.py` を実行してください。

`peek()` は待機せずに戻ります。スナップショットの取得に成功すると `0`、最初の通知を待っている間は
`-errno.EAGAIN`、エラー時はその他の負の errno を返します。プロパティは成功後に読み取ってください。
新しい通知がない場合は前回のスナップショットを返すため、更新の有無は `timestamp` を比較して判定します。

受信成功は、必ずしも測位成立を意味しません。`mode=3` / `status=1` は 3D 測位を示します。

| プロパティ | 意味 |
| --- | --- |
| `lat`, `lon` | 緯度・経度（度） |
| `alt` | 受信した高度（m）。追加のジオイド補正は行わない |
| `speed` | 速度（m/s） |
| `track` | 方位（度） |
| `mode`, `status` | 測位状態 |
| `pdop`, `hdop`, `vdop` | 精度低下率（DOP） |
| `used_sats`, `total_sats` | 衛星数。`used_sats` は追尾フラグを数えた値 |
| `timestamp` | UTC の Unix 時刻（秒） |

欠損値は `None` です。初期化時にデバイスを開けない場合は `OSError` が発生します。
現在、I2C ドライバーの衛星選択は固定設定です。プロトコルの違いは
[UART ガイドの I2C 衛星設定](uart.md#i2c-satellite-settings)を参照してください。

<a id="building-from-a-checkout"></a>

## リポジトリからのビルド

ドライバーを変更する場合や C の実行ファイルを使う場合は、リポジトリを取得します。
通常の Python 利用では、[README](../../README.ja.md) に従って PyPI からインストールしてください。

```sh
sudo apt install git build-essential python3-dev python3-venv libgpiod-dev gpiod
git clone https://github.com/hishizuka/rpi-cxd56xx-gnss.git
cd rpi-cxd56xx-gnss
python3 -m venv .venv
. .venv/bin/activate
CXD56XX_GPIO_CHIP=/dev/gpiochip0 python -m pip install .
```

[セットアップ手順](setup.md)で確認した GPIO チップを指定してください。
ソースやビルド設定を変更した後は、`--force-reinstall --no-cache-dir` を付けて再インストールします。
Cython 拡張はインストール時にビルドされます。

## C の実行ファイルとライブラリ

取得したリポジトリで実行します。

```sh
make CXD56XX_GPIO_CHIP=/dev/gpiochip0
./build/cxd5610
```

引数なしの `make` は、環境変数 `CXD56XX_GPIO_CHIP` が設定されていればその値を、なければデフォルトのチップを使います。
設定の変更を反映するため、実行するたびに再ビルドします。

C アプリケーションに組み込むには、`CXD5610_NO_MAIN=1` を指定して `cxd5610_rpi.c` をコンパイルし、
`gpiod`・`pthread`・`m` をリンクしてください。API と上書き可能なデフォルト値は
[cxd5610_rpi.h](../../src/cxd56xx_gnss/cxd5610_rpi.h) に宣言されています。
