# rpi-cxd56xx-gnss

```sh
pip install rpi-cxd56xx-gnss
```

![Raspberry Pi と Sony Spresense GNSS Add-on Board](https://raw.githubusercontent.com/hishizuka/rpi-cxd56xx-gnss/main/docs/images/rpi-cxd56xx-gnss.jpg)

[English](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/README.md) | 日本語

Raspberry Pi で Sony CXD56xx GNSS を利用するための Linux I2C ドライバー、Python バインディング、コマンドラインツールです。
`CXD56xx` クラスまたは `cxd56xx-gnss` コマンドで、位置・高度・速度・方位・衛星情報・UTC 時刻を取得できます。
Python APIにはPOSIX UART用の[`CXD56xxUART`](docs/ja/uart-api.md)も追加しています。
位置と生RTCMを取得できます。CLIは引き続きI2C用です。
I2C用Python APIでは[QZSS災危通報の生データ](docs/ja/api.md#qzss-災危通報i2c)も取得でき、
別途インストールしたazarashiへ直接渡せます。L1Sと災危通報は起動時・稼働中にON/OFFできます。
`wait_ready()` で設定適用を確認でき、再生成が必要な受信停止は `CXD56xxSessionError` で判別できます。

I2Cドライバーには**Linux、Python 3.13 以上、libgpiod 2.0 以上**が必要です。
パッケージは利用環境でソースからビルドされます。Raspberry Pi OS Trixie 以降を推奨します。

## クイックスタート

[セットアップ手順](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/docs/ja/setup.md)に従い、
**SDA・SCL・IRQIN を 1.8 V にレベル変換して接続**し、I2C を **400 kHz** に設定して I2C/GPIO のアクセス権限を付与してください。

```sh
sudo apt install build-essential python3-dev python3-venv libgpiod-dev gpiod
python3 -m venv .venv
. .venv/bin/activate
CXD56XX_GPIO_CHIP=/dev/gpiochip0 python -m pip install --no-cache-dir rpi-cxd56xx-gnss
cxd56xx-gnss
```

`/dev/gpiochip0` は、`gpiodetect` と `gpioinfo` で確認した、割り込み用 GPIO を含むチップに置き換えてください。
デフォルトは `/dev/gpiochip4`、ラインオフセットは `17` です。
チップはビルド時に選択するため、変更には再インストールが必要です。終了は **Ctrl+C** です。

## I2C の動作確認済みハードウェア

| ボード | チップ | バンド | 確認内容 |
| --- | --- | --- | --- |
| [Spresense GNSS Add-on Board](https://developer.spresense.sony-semicon.com/products/gnss-add-on-board) | CXD5610GF | L1 + L5 | Pi Zero 2 W で 3D 測位を確認 |

CXD5605 の I2C 接続での互換性は未確認です。

## UART Python APIと独立サンプル

チェックアウトから`CXD56XX_BUILD_I2C=0 python -m pip install '.[uart]'`
でUART専用APIを導入できます。
[UART APIガイド](docs/ja/uart-api.md)に起動・値の定義・RTCMを記載しています。
I2Cと同じDCR設定・受信APIを提供します。UARTのDCR出力設定はv00.201のASCII出力向けです。
対応はFWに依存し、有効化には診断電文も含む出力マスクを使用します。

リポジトリには、起動コマンド送信・バンド選択・生の NMEA 出力を行う、独立した pySerial の
[UART サンプル](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/examples/uart_read.py)を含みます。
このサンプルは **pip ではインストールされず**、`CXD56xx` API も使用しません。

以下のボードで UART 接続の試験実績があります。

| ボード | チップ | バンド |
| --- | --- | --- |
| [REYAX RYS8833_Lite](https://reyax.com/product/GPS-GNSS/RYS8833) | CXD5605AGF | L1 |
| [REYAX RYS8839_Lite](https://reyax.com/product/GPS-GNSS/RYS8839) | CXD5610GF | L1 + L5 |

## 追加ツール

リポジトリまたは展開したソース配布物から実行するツールです。pipのコマンドとしてはインストールされません。

- [RTCM収集](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/docs/ja/rtcm.md)：I2C観測データの取得・検証と、screenでの継続収集に対応します。
- [ファームウェア更新](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/docs/ja/firmware-update.md)：別途取得したv144パッケージのオフライン検査と、Spresense GNSSアドオンへの明示的なI2C書き込みに対応します。別ツールのRYS8839向けUART更新は実験用で、ファームウェア互換性は未確認です。

## ドキュメント

- [セットアップ](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/docs/ja/setup.md) — 接続図、ピン対応表、I2C クロックと GPIO の設定。
- [Python・C API](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/docs/ja/api.md) — 使用例、プロパティ、リポジトリからのビルド。
- [UART サンプル](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/docs/ja/uart.md) — 独立した pySerial サンプルの使い方、起動コマンド、バンド選択。

## ライセンスと由来

ライセンスは [Apache License 2.0](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/LICENSE) です。
[Pi Zero Bikecomputer](https://github.com/hishizuka/pizero_bikecomputer) の GNSS 実装を切り出した独立したライブラリです。
元のアプリケーションは、現在このライブラリを外部パッケージとして利用しています。
Apache NuttX を元にしたコードを含みます。詳細は
[NOTICE](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/NOTICE) と
[THIRD_PARTY.md](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/THIRD_PARTY.md) を参照してください。
本プロジェクトは Sony および Apache Software Foundation とは関係のない独立したプロジェクトです。
