# UART の起動とバンド選択

[English](../uart.md) | 日本語

[README](../../README.ja.md) · [I2C セットアップ](setup.md) · [Python・C API](api.md)

別ツールの実験用RYS8839ファームウェア更新については、
[更新ガイド](firmware-update.md#experimental-uart-update)を参照してください。

[examples/uart_read.py](../../examples/uart_read.py) は、REYAX RYS8833_Lite / RYS8839_Lite に
起動コマンドを送信し、NMEA 出力を表示するサンプルです。
pySerial を直接使用するため、I2C 用のネイティブ拡張や libgpiod は不要です。

この独立したサンプルは pip ではインストールされず、`CXD56xx` API も提供しません。
パッケージの API と CLI は **I2C のみ対応**です。

## 接続と実行

Raspberry Pi では、`sudo raspi-config` でシリアルログインシェルを無効にし、シリアルハードウェアを有効にします。
REYAX ボードの UART 信号は 1.8 V です。レベル変換を介してホストの TX とボードの RX、
ホストの RX とボードの TX を接続し、GND を共通にしてください。電源とピン配置は
[RYS8833_Lite](https://reyax.com/upload/products_download/download_file/RYS8833_Lite.pdf) と
[RYS8839_Lite](https://reyax.com/upload/products_download/download_file/RYS8839_Lite_EN.pdf) の製品資料を確認してください。

サンプルをダウンロードするか、リポジトリを取得して実行します。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install pyserial

# RYS8833_Lite: L1
python examples/uart_read.py /dev/ttyS0 --gns 47

# RYS8839_Lite: L1 + L5
python examples/uart_read.py /dev/ttyS0 --gns 16367
```

使用するシリアルデバイスのパスに置き換えてください。デフォルトは **115200 bps、8N1** です。
受信機の設定が異なる場合は、例えば `--baudrate 9600` で変更できます。
初期化後は NMEA を表示し続け、**Ctrl+C** でポートを閉じます。
`--init-only` は測位を開始した時点で終了します。終了時に `@GSTP` は送信しないため、受信機は測位を継続します。

## 起動コマンド

UART 接続でも、起動時に測位を停止し、衛星選択を設定してから測位を再開する必要があります。
RYS8839_Lite の場合は次の順序です。

```text
@GSTP
@GGNS
@GNS 16367
@GGNS
@GSR
```

各コマンドの末尾には **CRLF（`\r\n`）**を付け、`[command] Done` の応答を待ってから次のコマンドを送信します。
サンプルはこの手順を自動で実行し、各コマンドの応答を最大 5 秒待ちます。
`Err` 応答またはタイムアウトが発生した場合は、以降のコマンドを送信せずに終了します。

| コマンド | 目的 |
| --- | --- |
| `@GSTP` | 測位を停止し、設定用の Idle 状態にする |
| `@GGNS` | 変更前・変更後の衛星設定を読み取る |
| `@GNS <mask>` | ビットマスクで衛星システムと信号を選択する |
| `@GSR` | ホットスタートで測位を開始する |

参考：[RYS8833_Lite Quick Start Guide](https://reyax.com/upload/products_download/download_file/RYS8833_Lite.pdf#page=6)、
[RYS8839 Software Guide](https://reyax.com/upload/products_download/download_file/RYS8839_Software_Guide.pdf#page=9)。
これらの ASCII コマンドは UART 用です。I2C ドライバーはバイナリープロトコルを使用します。

## バンドと衛星システムの選択（`--gns`）

使用する信号のビットを `1` にします。ビット `n` の値は `1 << n` で、ビット OR によって組み合わせます。
重複しないビット値を足し合わせても同じ結果になります。サンプルは 10 進数と `0x` で始まる 16 進数のマスクを受け付けます。

以下は UART の `@GNS` に対する割り当てです。
[RYS8839 Software Guide §3.3.37](https://reyax.com/upload/products_download/download_file/RYS8839_Software_Guide.pdf#page=40) と
[RYS8830/RYS8833 Software Guide §12](https://reyax.com/upload/products_download/download_file/RYS8830_RYS8833_Software_Guide.pdf) に記載されています。

| ビット | 10 進数（16 進数） | 信号 |
| --- | --- | --- |
| 0 | `1` (`0x0001`) | GPS L1-C/A |
| 1 | `2` (`0x0002`) | GLONASS L1OF |
| 2 | `4` (`0x0004`) | SBAS (L1) |
| 3 | `8` (`0x0008`) | QZSS L1-C/A |
| 4 | `16` (`0x0010`) | 予約ビット。0 のままにする |
| 5 | `32` (`0x0020`) | QZSS L1-S |
| 6 | `64` (`0x0040`) | BeiDou B1I |
| 7 | `128` (`0x0080`) | Galileo E1B/C |
| 8 | `256` (`0x0100`) | GPS L5 |
| 9 | `512` (`0x0200`) | QZSS L5 |
| 10 | `1024` (`0x0400`) | BeiDou B1C（L1 帯） |
| 11 | `2048` (`0x0800`) | BeiDou B2a（L5 帯） |
| 12 | `4096` (`0x1000`) | Galileo E5a（L5 帯） |
| 13 | `8192` (`0x2000`) | NavIC (L5) |

CXD5605（RYS8833_Lite）は、ビット 4 を除くビット 0–7 の L1 信号を使用します。
ビット 8–13 は CXD5610（RYS8839_Lite）が対象です。**ビット 10 の BeiDou B1C は L1 信号**であり、
L5 のビットは 8、9、11、12、13 です。

**RYS8833 では GLONASS（ビット 1）と BeiDou（ビット 6）を同時に選択できません。**
BeiDou の利用可否はファームウェアにも依存します。`239`（`0x00ef`）は両方を有効にするため、RYS8833 では使用しないでください。

| モジュール | マスク | 選択内容 |
| --- | --- | --- |
| RYS8833 / RYS8839 | `1` (`0x0001`) | GPS L1-C/A |
| RYS8833 / RYS8839 | `47` (`0x002f`) | GPS L1、GLONASS、SBAS、QZSS L1-C/A・L1-S |
| RYS8833 / RYS8839 | `109` (`0x006d`) | GPS L1、BeiDou B1I、SBAS、QZSS L1-C/A・L1-S |
| RYS8833 / RYS8839 | `173` (`0x00ad`) | GPS L1、Galileo E1、SBAS、QZSS L1-C/A・L1-S |
| RYS8839 | `813` (`0x032d`) | GPS L1/L5、QZSS L1-C/A・L1-S・L5、SBAS |
| RYS8839 | `1263` (`0x04ef`) | BeiDou B1C を含む、表にあるすべての L1 信号 |
| RYS8839 | `16367` (`0x3fef`) | 表にあるすべての信号。ビット 4 を除くビット 0–13 |

例えば `47 = 1 + 2 + 4 + 8 + 32` です。GPS/QZSS の L1/L5 と SBAS を選択する場合は、次のように計算できます。

```python
# GPS L1 + SBAS + QZSS L1-C/A + QZSS L1-S + GPS L5 + QZSS L5
gns = (1 << 0) | (1 << 2) | (1 << 3) | (1 << 5) | (1 << 8) | (1 << 9)
print(gns, hex(gns))  # 813 0x32d
```

```sh
python examples/uart_read.py /dev/ttyS0 --gns 0x032d
```

設定変更は測位を停止した状態で行い、その後 `@GGNS` で確認してください。サンプルはこの処理を自動で行います。
実際の受信状況は、アンテナの対応バンドと受信環境にも依存します。

<a id="i2c-satellite-settings"></a>

## I2C の衛星設定

`CXD56xx` クラスと [I2C サンプル](../../examples/i2c_read.py)は、
[稼働中も切り替えられるQZSS L1S・災危通報設定](api.md#qzss-災危通報i2c)を除き、Cドライバー内の固定設定を使用します。
`--gns` は UART サンプル用の引数です。その他のI2C衛星設定を変更する場合は、受信機のバイナリープロトコル仕様に従って
[cxd5610_rpi.c](../../src/cxd56xx_gnss/cxd5610_rpi.c) の `configure_dcreport()` 内にある `sat_setting` を変更し、再ビルドしてください。
UART のマスクをそのままバイナリープロトコルの設定に代入しないでください。
