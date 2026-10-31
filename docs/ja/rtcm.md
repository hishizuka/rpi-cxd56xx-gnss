# I2C の RTCM 観測データ収集

[English](../rtcm.md) | 日本語

[Python・C API](api.md) · [UART サンプル](uart.md) · [ファームウェア更新](firmware-update.md)

ここで使うのは独立した収集ツールです。通常の `CXD56xx` APIやインストール済みCLIにはRTCM取得機能を追加しません。
以下はSpresense GNSSアドオンでの収集手順です。他機種・ファームウェア版への適用は別途確認が必要です。
更新ツールの依存関係と使い方は、[専用ガイド](firmware-update.md)を参照してください。

<a id="i2c-firmware-update"></a>

## 対応範囲

Spresense GNSS Add-on Boardのv00.144でRTCM取得に対応しています。
v00.103はRTCM通知の有効化を拒否するため、この手順では収集できません。
更新手順は[ファームウェア更新ガイド](firmware-update.md)を参照してください。
RTCMの受信成功やRINEXへの位相フィールドの出力だけで、連続した有効な搬送波追尾やRTK・PPKの精度は判断できません。

## 1コマンドで収集する（screen）

v144のSpresense GNSSアドオンでは、リポジトリ内の次のコマンドで収集できます。
Linux、Cコンパイラー、libgpiod 2.0以降と `screen` が必要です。
通常のPython拡張は不要で、リポジトリまたは展開したソース配布物のルートから実行します。

```sh
sudo apt install build-essential libgpiod-dev gpiod screen
```

```sh
./scripts/gnss-record
```

- 起動すると `gnss-rtcm` というscreenセッション内で収集を続けます。
- **デタッチして継続：** Ctrl+Aを押して離し、Dを押します。SSHを切断しても収集は続きます。
- **再接続：** 同じ `./scripts/gnss-record` を実行します。
- **終了：** screen内でCtrl+Cを押します。測位停止の応答を待ち、RTCMを無効にして保存します。
- **状態と最新ログの場所：** `./scripts/gnss-record status` を実行します。

端末なしで起動する場合は `./scripts/gnss-record --detach`、時間を区切る場合は
`./scripts/gnss-record --duration 600` を使います。既存のscreen内で直接動かす場合は
`./scripts/gnss-record --foreground` です。SSH越しに対話起動する場合は端末を割り当ててください。

IRQのGPIO17はライン名からGPIOチップを自動検出します。I2Cは `/dev/i2c-1`・`0x24`、
RTCMはSonyサンプルの `0x07102202`、信号選択は通常ドライバーと同じ `0x3fef` を適用します。
起動時の測位・通知設定はセッション内だけで変更し、設定の保存やファームウェア更新は行いません。
400kHzや1.8Vレベル変換などの配線・OS設定は[セットアップ](setup.md)を参照してください。

ログはGit管理外の `~/.local/state/rpi-cxd56xx-gnss/recordings/` に保存され、
`latest` が最新の保存先を指します。`notifications.bin` は受信中に逐次保存され、
Ctrl+Cで終了すると `observations.rtcm3` と `summary.json` が完成します。
位置情報を含むため、ログをGitへ追加しないでください。

`GNSS_GPIO_CHIP`・`GNSS_IRQ_LINE`・`GNSS_I2C_DEVICE`・`GNSS_I2C_ADDRESS` の環境変数で
接続先を上書きできます。GPIOチップを明示した場合もラインの存在を確認します。
`GNSS_PYTHON` はPython、`GNSS_SCREEN_SESSION` はscreenのセッション名を選びます。
実行中のセッションがある場合は再接続するため、新しい引数は次の収集開始時に適用されます。

## 再実行用の短時間テスト

`examples/i2c_rtcm_test.py` は専用のCプローブをログディレクトリ内にビルドして実行します。
既存のCドライバーのI2C・IRQ・チェックサム処理を利用し、通常のPython拡張は変更しません。
プローブだけ通知バッファを4096バイトに拡張します。既存ドライバーの1536バイトでは、
プロトコル上の大きなRTCM通知を格納できないためです。

```sh
python3 examples/i2c_rtcm_test.py \
  --gpio-chip /dev/gpiochip0 --irq-line 17 --duration 30
```

Sonyサンプルと同じGPS/QZSS/GalileoのMSM7・エフェメリス設定は、
`--rtcm-mask 0x07102202` で指定できます。送信する4バイトは `02 22 10 07` です。

```sh
python3 examples/i2c_rtcm_test.py \
  --gpio-chip /dev/gpiochip0 --irq-line 17 --duration 30 \
  --rtcm-mask 0x07102202
```

チップ名は `gpiodetect` / `gpioinfo` で確認した値に置き換えてください。
Linux、Cコンパイラー、libgpiod 2.0以上の開発ファイルが必要です。
同じモジュールを使用する別プロセスは終了してください。

RTCM通知の設定が拒否された場合は測位を開始せず終了します。
通知数による制限と区別するため、`0x84` が拒否されたときは通知の組合せを小さくして再試行します。
終了時には測位を停止し、RTCM出力を無効にして通常ドライバーの通知設定へ戻します。
バックアップ保存やファームウェア更新は行いません。
`--gnss-mask` で変更した揮発性の衛星選択は以前の値へ戻しません。通常ドライバーは次の起動時に固定設定を再適用します。

ログはGit管理外の `~/.local/state/rpi-cxd56xx-gnss/rtcm-<UTC時刻>/` に保存します。
`$XDG_STATE_HOME` があればその場所を使用します。`--log-dir` で新規ディレクトリも指定できます。

| ファイル | 内容 |
| --- | --- |
| `events.jsonl` | コマンド要求、応答、終了時の復元結果 |
| `notifications.bin` | チェックサム検証済み受信パケットを再構成した記録。位置情報を含み得る |
| `rtcm-stream.bin` | `0x84` の通知ヘッダーを除いて受信順に連結したデータ。v144のヘッダーは3バイト、REYAX仕様は5バイト |
| `observations.rtcm3` | CRC24Qの検証に通ったRTCMフレーム |
| `fragment-metadata.json` | 通知形式、宣言サイズと実サイズ。REYAX形式では分割数・通番も記録 |
| `summary.json` | コマンド応答、RTCM種類別の件数、CRCエラー、欠落／余剰バイトなど |

CRCが正常でも、丸ごとのフレーム欠落や測定値の有効性までは保証しません。
取得に成功した場合は、通番の連続性、MSMの測定値、RTKLIBでの変換結果も確認してください。
終了コードは成功が `0`、コマンド拒否・ビルド／転送／検証エラーが `1`、
正常終了したもののRTCM未受信の場合が `2` です。
正常に終了処理できたCtrl+Cは `130`、SIGTERMは `143` を返します。終了処理に失敗した場合は `1` です。
