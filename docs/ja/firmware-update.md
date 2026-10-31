# ファームウェア更新ツール

[English](../firmware-update.md) | 日本語

[README](../../README.ja.md) · [I2Cセットアップ](setup.md) · [UART](uart.md) · [RTCM](rtcm.md)

`examples/` にある独立したツールです。リポジトリまたは展開したソース配布物のルートから実行します。
pipのコマンドとしてはインストールされません。Piから直接更新する部分は本プロジェクトの実装です。
コピーして使う場合は `examples/` をまとめて配置してください。3種類の更新ツールは、
同じディレクトリの `_firmware_common.py` を読み込みます。
Sonyの[公式更新手順](https://developer.spresense.sony-semicon.com/ja/downloads/gnss-addon-fw-updater/manual)はSpresenseメインボードを使用します。

| ツール | 対象 | 機能・制約 |
| --- | --- | --- |
| `i2c_firmware_update.py` | Spresense GNSS Add-on Board、CXD5610GF | v144の5イメージ更新と、実験用のv201コード＋元のSony CFG再投入プロファイル |
| `uart_firmware_update.py` | 実験用のRYS8839/CXD5610GF向け経路 | CFG・消去命令なしでv144コードを書込み。ファームウェア互換性は未確認。RYS8833/CXD5605は対象外 |
| `i2c_firmware_update_preserve.py` | CFGを送らないI2C更新・検査 | 検証済みのSony v144／別途取得したv201コードだけを受け付ける実験用経路。CFG・消去・設定書込みを送信段階でも拒否 |

各ツールとも既定ではオフライン検査だけを行い、**`--write` を指定したときだけ書き込みます**。
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
Spresense v00.103では拒否される場合があります。拒否された場合は停止し、自動で方式を切り替えません。
別の実行で `--write --enter-mode erase` を指定すると、**eMRAMを消去**してから
RAM上の更新プログラム、SDK、アプリ、ライブラリ、**Spresense専用設定**を転送・書き込みします。
この方式はSpresense専用です。自動バックアップ・ロールバック機能はありません。

転送と各イメージ書き込みの応答を確認し、最後のリセット後に `v00.144` を読み取れた場合だけ成功とします。
eMRAM全体の読み戻し比較は実装していません。更新後は通常リーダーと、必要に応じて
[RTCM収集](rtcm.md)を別々に確認してください。

`v144 --write --enter-mode erase` でも、更新モード中だけSony実装と同じ100ms待機後のIRQ High確認を使い、
ヘッダーから本文までIRQがHighのままになるブートローダーに対応します。通常測位には適用しません。
消去要求の応答待ちでタイムアウトし、イメージ転送がまだ始まっていない場合に限り、
同じ入力と版の失敗ログを `--resume-after-erase /path/to/failed-update-log` で照合して再開できます。
ライブのブートローダー判定後にRAM updaterから進み、再消去・STOP・途中イメージの自動再送は行いません。

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

各ツールが `events.jsonl`・`summary.json` を保存します。UART版の `uart-rx.bin` には
NMEA座標が含まれる可能性があります。引数とローカルパスも記録するため、Git管理外に置き、共有前に内容を確認してください。
新規ログディレクトリは所有者のみアクセスでき、既存ディレクトリは再利用しません。
I2C版の既定保存先は `$XDG_STATE_HOME/rpi-cxd56xx-gnss/`、未設定時は `~/.local/state/rpi-cxd56xx-gnss/` 配下です。

終了コード `0` は要求した操作の完了、`1` は捕捉した更新・照会エラーや中断、
`2` は引数エラーです。不正な入力ファイルはデバイスを開く前に失敗します。
オフライン時の `completed=true` は検査完了だけを表します。書き込み時は `verified_version` も確認してください。
失敗した場合はログを保全し、最後に応答を確認した段階と受信機の状態を調べてから復旧を検討します。
各ツールともファームウェアの自動再送・エラー後の自動消去やリセット・書き込み済みイメージの巻き戻しは行いません。

## CFGを送らないI2Cコード更新

`examples/i2c_firmware_update_preserve.py` は既存の5イメージ更新とは別の経路です。
`--profile v144` または `--profile v201` で、固定したファイル名・サイズ・SHA-256・更新先版を選びます。
ファームウェアは同梱しません。別途取得したv201の別基板での動作は実機ごとの検証が必要です。

入力ディレクトリには、選択した版のRAM updater・SDK・APP・LIBの4ファイルだけを置きます。
v201のupdater名は `cxd5610_updater.efpk`、v144は `updater.efpk` です。
CFGを含むディレクトリ、異なる版の混在、不明な追加ファイルはデバイスを開く前に拒否します。
任意のイメージへ検査を緩めるオプションはありません。

```sh
python3 examples/i2c_firmware_update_preserve.py \
  --profile v201 --image-dir "/path/to/audited-v201-code-only"
```

既定はオフライン計画で、249フレームの一覧と書込み先 `000000`・`010000`・`020000` を記録します。
版と読出し可能な運用設定だけを取得する場合：

```sh
python3 examples/i2c_firmware_update_preserve.py \
  --profile v201 --query --gpio-chip /dev/gpiochip0 --irq-line 17
```

対象実機の現在版・配線・入力を確認した上で実験用更新を実施する場合：

```sh
python3 examples/i2c_firmware_update_preserve.py --write \
  --profile v201 --image-dir "/path/to/audited-v201-code-only" \
  --expected-version v00.144 --gpio-chip /dev/gpiochip0 --irq-line 17
```

`--expected-version` は更新前の版です。GPIOチップは実機で確認した値を指定します。
ForceBootが拒否された場合は停止します。消去への切替は実装していません。
RAM updaterの全フレーム転送と実行ACKを確認し、通常FWがまだ版照会へ応答する場合はSDK等を書きません。
SDK／APP／LIBの書込み応答をすべて確認した後、更新先版の独立した照会が成功した場合だけ更新完了とします。

起動直後のForceBootを試す場合は `--wait-for-reset 120` を指定し、
`waiting_for_physical_reset` が記録されてから、Piを起動したままGNSSだけを物理リセットします。
ツールはリセットGPIOを操作しません。待機中は通常FWの完全な拒否応答（`ea`／-22、-2）と、
コマンド書込み時のNACK／EIO、起動通知 `01`／`02` だけを扱い、ForceBoot要求のみ繰り返します。
IRQタイムアウト・読出しエラー・短い書込み・不正パケットでは停止し、イメージを再送しません。
正常な移行応答の後も、RAM転送・実行と通常FWが応答しないことの確認を経てからコードを書きます。

別の明示的な移行方法として `--reboot-before-force` は非消去の `0x00 / 01` を送り、
その応答後にForceBootを要求します。物理リセット待機とは併用できません。
Spresense v00.144ではForceBootが `ea`（-22）で拒否される場合があり、
拒否された場合はこのコード専用経路で更新を継続できません。
後述の「元のSony CFGを再投入するv201更新」は、消去を許可して同一CFGを戻す別の経路です。

送信ログはCFG書込み命令・消去命令を送っていないことを監査できます。
既存CFG全体の読戻し機能はなく、全バイトの不変性や新SDKとの互換性まで保証するものではありません。
更新前後の `--query` と通常測位・RTCMの確認を併用してください。TCXO値など動的な値は固定設定と区別します。

## 元のSony CFGを再投入するv201更新

`i2c_firmware_update.py --profile v201` はSpresense専用の実験用プロファイルです。
別途取得したv201のRAM updater・SDK・APP・LIBと、公式Sony v144パッケージの `spresense_i2c.cfg`
（3,008バイト、SHA-256 `f903fe844e52d9d014eb86a4d72150d6b53ca9cae8857f6789562838b7eb7b0c`）を組み合わせます。
Spresense用のCFGを再投入する構成で、別基板用CFGは使用しません。
CFGファイルの内容を維持しますが、手順は **eMRAM消去・コード書込み・Sony CFG再書込み** を伴います。
実機内のCFG全バイトを読戻した構成ではなく、このv201との組合せの動作は実機確認が必要です。
CFG領域へ書かない `i2c_firmware_update_preserve.py` とは別の操作です。

入力の検査と送信計画だけを作る場合：

```sh
python3 examples/i2c_firmware_update.py \
  --profile v201 --enter-mode erase \
  --package "/path/to/gnss_addon_fw_updater_v144.zip" \
  --code-dir "/path/to/audited-v201-code-only"
```

計250転送フレームと、SDK・APP・LIB・CFGの書込み引数
`000000`・`010000`・`020000`・`030100` を確認してください。
デバイスは開きません。Sonyパッケージ全体とv201コード4ファイルを固定ハッシュで検証し、
v201コード用ディレクトリへのCFG混入や、別CFGへの差替えは拒否します。

消去と元のSony CFGの再投入を選んで実行する場合のみ、上のコマンドに
`--write --expected-version v00.144 --gpio-chip /dev/gpiochip0 --irq-line 17` を追加します。
GPIOは実機で確認した値を使います。v201書込みには明示的な `--enter-mode erase` が必要です。
消去は入力検査と現在版の照会の後に行い、各ACKと最後の `v00.201` の照会が揃った場合だけ成功とします。
エラー後の自動再送・消去・リセット・ロールバックは行いません。

消去要求の応答がタイムアウトした場合、消去されていないとは判断できません。
電源を保ったまま応答と受信境界を確認してください。ブートローダーでは、ヘッダーと本文の間で
IRQがHighのままになる場合があります。このプロファイルは更新モード中の本文だけ、
Sony実装と同じ100msの待機後にHighを確認して、ヘッダーで宣言された長さを読みます。
通常測位の受信にはこの処理を適用しません。

消去要求でタイムアウトし、イメージ転送がまだ始まっていない場合に限り、
上の書込みコマンドへ `--resume-after-erase /path/to/failed-update-log` を追加して再開できます。
前回ログの版・入力ハッシュ・最後の消去要求を照合し、さらに実機が版照会へ
`0xff / 06 fe`（未実装 -2）を返すことを確認してからRAM updaterを送ります。
通常版が応答する場合や別エラーの場合は転送しません。再開時はSTOP・消去を送りません。
これは途中のイメージを再送する機能ではなく、転送開始後のエラーには使用できません。

この実験用プロファイルは、別途取得したv201ファームウェアのSony公式配布・公式対応を意味しません。
