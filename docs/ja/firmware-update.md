# ファームウェア更新ツール

[English](../firmware-update.md) | 日本語

[README](../../README.ja.md) · [I2Cセットアップ](setup.md) · [UART](uart.md) · [RTCM](rtcm.md)

`examples/` にある独立したツールです。リポジトリまたは展開したソース配布物のルートから実行します。
pipのコマンドとしてはインストールされません。Piから直接更新する部分は本プロジェクトの実装です。
Sonyの[公式更新手順](https://developer.spresense.sony-semicon.com/ja/downloads/gnss-addon-fw-updater/manual)はSpresenseメインボードを使用します。

| ツール | 対象 | 確認状況 |
| --- | --- | --- |
| `i2c_firmware_update.py` | Spresense GNSS Add-on Board、CXD5610GF | I2Cでv00.103 → v00.144へ更新した[実機記録](rtcm.md#i2c-firmware-update)あり |
| `uart_firmware_update.py` | 実験用のRYS8839/CXD5610GF向け経路 | ファームウェア互換性・実機での更新成功は未確認。RYS8833/CXD5605は対象外 |

どちらも既定ではオフライン検査だけを行い、**`--write` を指定したときだけ書き込みます**。
I2C版には、現在の版を読み取るだけの `--query` もあります。

## 入力と依存関係

[Sony公式v144パッケージ](https://developer.spresense.sony-semicon.com/file/download/gnss-add-on-fw-updater-v144)を
Sonyの使用条件に従って別途取得し、Git管理外に保存してください。ファームウェアは同梱しません。
`--package` は原版ZIPまたはその中のSPKを受け付けます。デバイスを開く前に、
パッケージ全体と各イメージのサイズ・SHA-256を照合します。他の版や再圧縮した入力は拒否します。
I2C版の `--image-dir` は、スクリプトに列挙した抽出済み5イメージを同じハッシュ検査で読み込みます。

オフライン検査はPython標準ライブラリだけで動きます。I2Cの実機アクセスにはLinuxと
libgpiodのPythonバインディングv2以降、UART書き込みにはpySerialと排他アクセスに対応した
POSIXシリアルポートが必要です。Raspberry Pi OS Trixieのシステム側 `python3` を使う場合：

```sh
sudo apt install gpiod python3-libgpiod python3-serial
```

Debianの[GPIOバインディング](https://packages.debian.org/trixie/python3-libgpiod)と
[シリアルモジュール](https://packages.debian.org/trixie/python3-serial)を利用します。
仮想環境を使う場合は、そのPythonからもこれらをインポートできるようにしてください。
更新ツール自体は `cxd56xx_gnss` 拡張のインストールを必要としません。

I2Cでは1.8Vレベル変換を含む[配線・アクセス権限の設定](setup.md)を済ませます。
`gpiodetect` / `gpioinfo` で実際のIRQ接続先を確認してください。以下の `/dev/gpiochip0`・17は例です。
通常リーダー・RTCM収集など同じ受信機を使うプロセスを終了し、安定した電源を用意します。
ファームウェア書き込み中は電源を切ったり処理を中断したりしないでください。

## オフライン検査

```sh
python3 examples/i2c_firmware_update.py \
  --package "/path/to/gnss_addon_fw_updater_v144.zip"
```

I2C/GPIOは開きません。`summary.json` の `mode=offline_plan`、`device_opened=false`、
イメージ一覧とフレーム数を確認します。検査成功は入力の一致を意味し、他機種との互換性を保証しません。

## I2Cの版照会と書き込み

まずファームウェアを指定せず、現在の版だけを読み取ります。

```sh
python3 examples/i2c_firmware_update.py --query \
  --gpio-chip /dev/gpiochip0 --irq-line 17
```

I2Cの既定は `/dev/i2c-1`・`0x24` です。`--i2c-device`・`--address` で変更できます。
書き込み時は現在の版と `--expected-version`（既定 `v00.103`）の一致を要求します。
すでに `v00.144` の場合は書き換えません。

```sh
python3 examples/i2c_firmware_update.py --write \
  --package "/path/to/gnss_addon_fw_updater_v144.zip" \
  --gpio-chip /dev/gpiochip0 --irq-line 17 --expected-version v00.103
```

既定の `--enter-mode force` は消去コマンドを使わず更新モードへの移行を要求します。
試験したv00.103では拒否されました。その場合は停止し、自動で方式を切り替えません。
別の実行で `--write --enter-mode erase` を指定すると、**eMRAMを消去**してから
RAM上の更新プログラム、SDK、アプリ、ライブラリ、**Spresense専用設定**を転送・書き込みします。
この方式でSpresense実機の更新を確認しています。他機種への汎用的な復旧手順ではなく、
自動バックアップ・ロールバック機能はありません。

転送と各イメージ書き込みの応答を確認し、最後のリセット後に `v00.144` を読み取れた場合だけ成功とします。
eMRAM全体の読み戻し比較は実装していません。更新後は通常リーダーと、必要に応じて
[RTCM収集](rtcm.md)を別々に確認してください。

<a id="experimental-uart-update"></a>

## 実験用UART更新

UART版は、検査済みのSpresense用v144パッケージからRAM更新プログラム・SDK・アプリ・ライブラリを使い、
Spresense用設定を除外します。この制限だけでRYS8839との互換性が確認できたわけではありません。

まずオフラインで検査します。`--log-dir` はGit管理外の未作成ディレクトリを指定してください。

```sh
python3 examples/uart_firmware_update.py \
  --package "/path/to/gnss_addon_fw_updater_v144.zip" \
  --log-dir "$HOME/.local/state/rpi-cxd56xx-gnss/uart-plan-example"
```

実機での実験を明示的に選ぶ場合の書き込み例：

```sh
python3 examples/uart_firmware_update.py --write \
  --package "/path/to/gnss_addon_fw_updater_v144.zip" \
  --port /dev/serial0 --baudrate 115200 --expected-version v00.129 \
  --wait-for-reset 30 \
  --log-dir "$HOME/.local/state/rpi-cxd56xx-gnss/uart-write-example"
```

ポート・通信速度・現在の版を実機に合わせます。`v00.129` はツールの既定値で、互換性を保証する値ではありません。
UART電圧とシリアルコンソールの無効化は[UARTガイド](uart.md)を参照してください。
`@VER`・`@GSTP` の後、`--wait-for-reset` 秒間ForceBootを繰り返し要求します。
ツールはRESET GPIOを操作しません。必要な物理リセットは転送開始前のForceBoot移行時に行います。

消去と設定イメージの書き込みは禁止しています。RAM更新プログラムを転送・実行した後も通常版照会に応答する場合は停止します。
ファームウェアフレームは自動再送しません。最後の起動応答が欠けた場合も、独立した版照会で
`v00.144` を確認しない限り成功にしません。

`--resume-bootloader` は、前の中断後にブートローダー内にいると別途確認できた受信機専用です。
最初の版照会とForceBoot移行を省くため、このモードでは `--expected-version` の一致を確認しません。
想定外の版を回避するためや、状態不明のまま再試行するためには使用しないでください。

## ログとエラー時の扱い

両方が `events.jsonl`・`summary.json` を保存します。UART版の `uart-rx.bin` には
NMEA座標が含まれる可能性があります。引数とローカルパスも記録するため、Git管理外に置き、共有前に内容を確認してください。
新規ログディレクトリは所有者のみアクセスでき、既存ディレクトリは再利用しません。
I2C版の既定保存先は `$XDG_STATE_HOME/rpi-cxd56xx-gnss/`、未設定時は `~/.local/state/rpi-cxd56xx-gnss/` 配下です。

終了コード `0` は要求した操作の完了、`1` は捕捉した更新・照会エラーや中断、
`2` は引数エラーです。不正な入力ファイルはデバイスを開く前に失敗します。
オフライン時の `completed=true` は検査完了だけを表します。書き込み時は `verified_version` も確認してください。
失敗した場合はログを保全し、最後に応答を確認した段階と受信機の状態を調べてから復旧を検討します。
どちらもファームウェアの自動再送・エラー後の自動消去やリセット・書き込み済みイメージの巻き戻しは行いません。
