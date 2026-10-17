# I2C の RTCM 観測データ取得調査

[English](../rtcm.md) | 日本語

[Python・C API](api.md) · [UART サンプル](uart.md) · [ファームウェア更新](firmware-update.md)

ここで使うのは独立した収集ツールです。通常の `CXD56xx` APIやインストール済みCLIにはRTCM取得機能を追加しません。
以下はSpresense GNSSアドオンでの手順と実験記録です。他機種・ファームウェア版への適用は別途確認が必要です。
更新ツールの依存関係と使い方は、[専用ガイド](firmware-update.md)を参照してください。

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

## 公開コマンド表

[REYAX RYS8839 Software Guide](https://reyax.com/upload/products_download/download_file/RYS8839_Software_Guide.pdf)
には、CXD5610搭載モジュールのI2CバイナリープロトコルによるRTCM出力が記載されています。

| コマンド／通知 | 内容 | ガイドの節 |
| --- | --- | --- |
| `0x06` | ファームウェア版の取得 | 4.2.5 |
| `0x45` | MSM4/MSM7・エフェメリスなど出力種類の選択 | 4.2.36 |
| `0x34` | 周期通知の選択。RTCMには `0x84` を指定 | 4.2.22 |
| `0x84` | RTCMを格納した通知。先頭5バイトはサイズ・分割情報など | 4.2.45 |

同ガイドの4.4節はI2Cでの転送を説明しています。4.3節ではRTCM入力を非対応としています。
受信機自身への補正入力と、観測データをホスト上のRTKLIBで処理することは別の機能です。

これはRYS8839の仕様です。同じCXD5610を使うSpresense GNSS Add-on Boardでも、
搭載ファームウェアによる違いを実機で確認する必要があります。

## 実機結果：2026-10-16

Spresense GNSS Add-on Board、ファームウェア `v00.103`、Pi Zero 2 W、
I2C-1 / 400 kHz / `0x24`、GPIO17（`/dev/gpiochip0`）で確認しました。

| 要求 | 応答 |
| --- | --- |
| `0x06` | `00 76 30 30 2e 31 30 33 0d 0a`。先頭の成功コードに続く文字列は `v00.103` |
| `0x45`、ペイロード `22 22 f2 07` | `00`（受理） |
| `0x34`、ペイロード `80 81 82 83 84 89` | `ea`（符号付き8ビットで `-22` / `EINVAL`） |
| `0x34`、ペイロード `80 84` | 同じく `-22` |
| `0x34`、ペイロード `84` | 同じく `-22` |
| RTCM出力の無効化、通常通知 `80 81 82 83 89` への復元 | いずれも `00` |

`-22` は同ガイド8節で引数の範囲エラーと定義されています。
通常通知の設定は成功し、RTCMを単独で指定しても拒否されるため、
通知数の上限だけではこの結果を説明できません。
この構成では、記載された手順によるRTCM通知を有効にできず、搬送波位相は未取得です。
チップ自体の非対応や、別ファームウェアでの取得可否を断定する結果ではありません。

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
| `rtcm-stream.bin` | `0x84` の通知ヘッダーを除いて受信順に連結したデータ。実測v144は3バイト、REYAX仕様は5バイト |
| `observations.rtcm3` | CRC24Qの検証に通ったRTCMフレーム |
| `fragment-metadata.json` | 通知形式、宣言サイズと実サイズ。REYAX形式では分割数・通番も記録 |
| `summary.json` | コマンド応答、RTCM種類別の件数、CRCエラー、欠落／余剰バイトなど |

CRCが正常でも、丸ごとのフレーム欠落や測定値の有効性までは保証しません。
取得に成功した場合は、通番の連続性、MSMの測定値、RTKLIBでの変換結果も確認してください。
終了コードは成功が `0`、コマンド拒否・ビルド／転送／検証エラーが `1`、
正常終了したもののRTCM未受信の場合が `2` です。
正常に終了処理できたCtrl+Cは `130`、SIGTERMは `143` を返します。終了処理に失敗した場合は `1` です。

## REYAX以外の公開ソースの確認（2026-10-16）

### NuttXの内蔵GNSSと外付けCXD5610は取得経路が異なる

Sony SDKが参照するNuttXのリビジョン
`a98b7ce090a0db5aa36c0feced496592735c5c57` と、Apache本家の
`c3e180f3aeaa01bf10b9264d92eac5043c071f4c` を確認しました。

- [内蔵GNSSドライバー `cxd56_gnss.c`](https://github.com/SPRESENSE/nuttx/blob/a98b7ce090a0db5aa36c0feced496592735c5c57/arch/arm/src/cxd56xx/cxd56_gnss.c#L1756)
  は `CXD56_GNSS_IOCTL_RTK_START` を `fw_gd_rtkstart()` に渡します。
  RTK用ファームウェア名 `gnssfwrtk` の選択や、RTKデータを読むオフセットもあります。
  この処理はSpresense内蔵GNSS用であり、外付けCXD5610へのI2Cコマンドではありません。
- [アドオン用ドライバー `cxd5610_gnss.c`](https://github.com/SPRESENSE/nuttx/blob/a98b7ce090a0db5aa36c0feced496592735c5c57/boards/arm/cxd56xx/drivers/sensors/cxd5610_gnss.c#L1930)
  のIOCTL処理にはRTK用の分岐がありません。読み取り経路も通常GNSS情報と災危通報のみで、
  RTCMの `0x84` を扱う処理はありません。
- 同アドオンドライバーは未処理のIOCTLを `default: break` で通し、初期値の成功コードを返します。
  共通ヘッダーにRTKの定義があることや、IOCTLが成功したことだけでは対応の証拠になりません。

### SonyのNuttX派生の実装にはRTCM処理がある

[Sony SIoT-MajiproのCXD5610ドライバー](https://github.com/sony/SIoT-Majipro/blob/36d30533c9e419389b4367d2448bb6ce8aa2dfaa/drivers/gnss/cxd5610/cxd5610_gnss.c#L1450)
は、NuttX由来のコードをZephyr/I2C向けに移植した実装です。
`OPC_RTCM_OUTPUT_SET=0x45`、`OPC_RTCM_NOTIFY=0x84` の定義と、
RTCM通知の解析・GPS/Galileo/QZSSのMSM4データを格納する処理があります。

ただし、`cxd5610_gnss_set_notify()` 内の `OPC_RTCM_NOTIFY` はコメントアウトされています。
コードの存在だけで、その版でRTCM出力が正常に動いたと判断することはできません。

[同リポジトリのアプリケーション](https://github.com/sony/SIoT-Majipro/blob/36d30533c9e419389b4367d2448bb6ce8aa2dfaa/applications/watch/cxd5610/src/gnss_addon_main.c#L420)
では、GPS/QZSS/GalileoのMSM7とエフェメリスを選んでいます。
RTCMマスクは `0x07102202`、I2Cペイロードは `02 22 10 07` です。
この限定した選択と、今回の全システム選択との差は追加の比較対象です。

### ファームウェアの世代差と配布の制約

[Sony SDKのGNSSアドオンサンプル](https://github.com/sonydevworld/spresense/blob/7fd61b2c03f06a4ff0302b84c755e58c338788b2/examples/gnss_addon/gnss_addon_main.c#L428)
はQZQSMについて `v00.144` 以降を要求します。今回の実機 `v00.103` より後の版が
公開サンプルで想定されていることは確認できますが、これはQZQSMの条件です。
`v00.144` で搬送波位相やRTCMが取得できると示したものではありません。

[Spresense GNSSアドオンの公式ダウンロードページ](https://developer.spresense.sony-semicon.com/downloads/gnss-add-on-board)
では、これとは別に実際の `GNSS Add-on Board Updater V144` が配布されています。
後述の公式手順はSpresenseメインボードを経由するものです。

[Sony公開のCXD5610更新サンプル](https://github.com/sony/SIoT-Majipro/blob/36d30533c9e419389b4367d2448bb6ce8aa2dfaa/applications/fwupdate/cxd5610/README.rst#L38)
はMAJIPRO CORE01 + GNSS01向けです。このリポジトリには更新用ファームウェアを含まず、
バイナリーの作成・入手方法もサポートしないと明記されています。
この制約は上記のSpresense用公式パッケージが存在しないという意味ではありません。

[SonyとSwift Navigationの2024-11-07の発表](https://www.swiftnav.com/resource/press-release/swift-navigation-collaborates-with-sony-semiconductor-solutions-to-enable-precise-location-in-small-devices)
でもCXD5610とSpresense GNSS Add-on Boardを評価対象として挙げています。
ここで説明されているのはSkylark DxのMSM1補正利用であり、
今回のボードから搬送波位相を出力できるとする根拠にはなりません。

### SonyサンプルのRTCMマスクで再試験（2026-10-16）

実機のファームウェアは引き続き `v00.103` です。

| 要求・確認 | 結果 |
| --- | --- |
| Sonyサンプルと同じ `0x07102202` を指定 | `0x45` に `02 22 10 07` が送信されたことをログで確認 |
| `0x45` の応答 | `00`（受理） |
| `0x34` に `80 81 82 83 84 89` | `ea`（`-22`） |
| `0x34` に `80 84` | `ea`（`-22`） |
| `0x34` に `84` のみ | `ea`（`-22`） |
| RTCM通知・有効なRTCMフレーム | いずれも0件。通知の有効化で拒否されたため取得ループは未開始 |
| 終了時のRTCM無効化・通常通知への復元 | いずれも `00` |
| 復元後の既存I2Cサンプル | 約8秒で4回の通常データ表示、標準エラー0件。終了時は追尾6／通知7、未Fix |

最初の問い合わせは、再起動後にIRQがすでにHighの状態でファームウェア照会がタイムアウトしました。
既存サンプルで通常データを受信できることを確認してから再試験し、上記のコマンド応答を取得しました。
初回タイムアウトはRTCMマスク送信前に発生したもので、RTCM設定の拒否とは区別しています。

RTCMの出力種類をSonyサンプルと同じ選択に絞っても、今回の有効化拒否は変わりませんでした。
ファームウェアの書換えや永続設定の変更は行っていません。
実行コマンド・応答・位置情報を含み得る生ログはGit管理外に保存しています。

## GNSSアドオンの公式ファームウェア更新方法（2026-10-16調査）

### 入手先と必要な機材

- [公式配布ページ：GNSS Add-on FW Updater v144](https://developer.spresense.sony-semicon.com/file/download/gnss-add-on-fw-updater-v144)
  は2024-02-22公開、ファイル名は `gnss_addon_fw_updater_v144.zip` です。
  ダウンロードにはSonyの使用許諾への同意が必要です。
- [Sony公式更新マニュアル（日本語）](https://developer.spresense.sony-semicon.com/ja/downloads/gnss-addon-fw-updater/manual)
  では、SpresenseメインボードにGNSSアドオンを取り付け、PCとUSBで接続します。
  現在のRaspberry PiからI2Cで直接書き換える手順は記載されていません。
- ZIP内の `.spk` はメインボードで動かす更新アプリです。
  SDKの通常アプリを書き換えたり、内蔵GNSS用 `gnssfw` を更新したりするだけでは、
  外付けCXD5610の更新にはなりません。

### 公式手順の要点

以下は手順の記録であり、今回実行したコマンドではありません。

1. アドオンをSpresenseメインボードに取り付け、メインボードをUSBでPCへ接続します。
2. ZIPを展開し、使用するOS用の `flash_writer` を同じフォルダーへ置きます。
   [Windows](https://github.com/sonydevworld/spresense/raw/master/sdk/tools/windows/flash_writer.exe)、
   [macOS](https://github.com/sonydevworld/spresense/raw/master/sdk/tools/macos/flash_writer)、
   [Linux](https://github.com/sonydevworld/spresense/raw/master/sdk/tools/linux/flash_writer)
   が案内されています。
3. [USBシリアルポートの確認手順](https://developer.spresense.sony-semicon.com/development-guides/?lang=en&page=sdk_set_up)
   に従い、実際のポート名を確認します。
4. macOS/Linuxでは、展開したフォルダーで次を実行します。
   `<USB_SERIAL_PORT>` は実機のポート名に置き換えます。

   ```sh
   chmod +x flash_writer
   ./flash_writer -c <USB_SERIAL_PORT> -d -n -e nuttx gnss_addon_fw_updater_v144.spk
   ```

5. 書き込み後に更新アプリが自動起動します。公式の目安はアプリ書き込み約2分、
   アドオン更新約1分です。処理中はLED0〜3が順番に点滅し、4個すべての点灯で完了、
   4個同時の繰り返し点滅はエラーです。書き込み・更新中は電源を切ったりリセットしたりしません。
6. 完了後、次のコマンドでメインボード上の更新アプリを削除します。

   ```sh
   ./flash_writer -c <USB_SERIAL_PORT> -d -n -e nuttx
   ```

メインボードの `nuttx` アプリはこの処理で置き換わります。使用していたアプリがあれば、
更新アプリの削除後に書き戻します。Windowsでは上記の `./flash_writer` を
`.\flash_writer.exe` に読み替えます。
`Not ACK` はターミナルを開き直して再実行、`Install failure. -28` は
ブートローダーをインストールして再実行する案内が公式マニュアルにあります。

更新後はI2C構成へ戻し、`0x06` の版確認、既存サンプルの通常測位、
Sonyサンプルのマスクを使った上記RTCMプローブを順に実行して、更新前と比較します。
今回の実機更新後の `v00.144` では、後述のとおり `0x84` の受信を確認しました。

### Raspberry PiのI2Cから直接更新できるか

**I2CによるCXD5610更新の公開実装はあります。Piへの移植は可能と考えられますが、
公開 `v144.spk` をそのままI2Cへ送って更新することはできません。**
以下はソースから確認した内容であり、Pi上での更新実績ではありません。

[Sony Majiproの `update_main.c`](https://github.com/sony/SIoT-Majipro/blob/36d30533c9e419389b4367d2448bb6ce8aa2dfaa/applications/fwupdate/cxd5610/src/update_main.c)
はUARTブリッジを介さず、ZephyrのI2C APIでCXD5610へ転送するApache-2.0の実装です。
アドレスは `0x24`、初期化時の速度はStandard-mode（100 kHz）です。
RESET出力とIRQ入力も使用します。この点は現在の400 kHz・SDA/SCL/IRQのみの接続と異なります。

更新処理は、更新モードへの移行、RAM上での更新プログラム実行、各イメージの書き込み、
リセットの順です。コマンドは `0x00`、`0x01`、`0x02`、`0x09`。
転送単位はファイルデータ最大4086バイトで、フレーム総数と1始まりの通番を付け、
各応答を確認します。書き込みのタイムアウトは30秒です。
通常測位ドライバーを並行動作させず、独立した更新ツールとして移植する必要があります。

[同サンプルのKconfig](https://github.com/sony/SIoT-Majipro/blob/36d30533c9e419389b4367d2448bb6ce8aa2dfaa/applications/fwupdate/cxd5610/Kconfig)
が指定する入力は次の5種類です。

| ファイル | 役割 |
| --- | --- |
| `cxd5610_updater.efpk` | RAMへ転送して実行する更新プログラム |
| `cxd5610_sdk.efpk` | SDKイメージ |
| `cxd5610_app.efpk` | GNSSアプリケーション |
| `cxd5610_lib.efpk` | ライブラリ |
| `cxd5610_EVB.cfg` | ボード用設定 |

これはMajiproの構成です。Spresenseアドオンの `v144` にも同じファイル構成・内容を
適用できると保証するものではありません。特にEVB用設定をそのまま流用しません。
公開サンプルにこれらのバイナリーは含まれず、Spresense用の単体配布先も未発見です。
ただし、後述のとおり公式 `v144.spk` の中からSpresense用の更新データを確保できました。

公式 `v144.spk` はメインボードで実行するアプリであり、上記のCXD5610用入力とは形式が異なります。
なお、[公開 `mkspk.c`](https://github.com/SPRESENSE/nuttx/blob/a98b7ce090a0db5aa36c0feced496592735c5c57/tools/cxd56/mkspk.c#L210)
はELFのロード領域をパッケージ化しCMACを付加する実装です。
SPKという拡張子だけから、内容が暗号化されていて抽出不能だと判断することもできません。
ユーザーの明示的な確認後、公式ダウンロード画面のEULAへ同意してZIPを取得しました。
この配布物に対する確認結果は後述します。ファームウェアのバイナリーはGitへ追加しません。

[Spresenseアドオンの公式回路図](https://github.com/sonydevworld/spresense-hw-design-files/blob/master/CXD5602PWBGNSS1/schematics/CXD5602PWBGNSS1_schematics.pdf)
では `RESET_X` の信号が `TP_RST` / `CL1` に出ています。通常のCN1/CN2配線には出ていないため、
Sonyサンプルと同じRESET制御を行うなら接続方法と電気条件の確認が必要です。
Pi向けの更新処理と実機での結果は、後述の「Pi向け更新ツールと実機試験」に記録します。
異常時にSDA/SCL/IRQの接続だけで必ず復旧できると保証するものではありません。

#### 公式v144パッケージの取得・データ検証

公式ZIPには `gnss_addon_fw_updater_v144.spk` が1個だけ含まれ、READMEや
単体のEFPKファイルは含まれていませんでした。

| 配布物 | サイズ（バイト） | SHA-256 |
| --- | ---: | --- |
| `gnss_addon_fw_updater_v144.zip` | 1,129,812 | `86609ae9e0041df6251034fa48b90401708cd63e1e457c4f2063b59e697901dd` |
| `gnss_addon_fw_updater_v144.spk` | 1,177,488 | `37d2294b8edd21ff24596333e850cedc7c7cc8e9de73c42129f93336e7ed2b2c` |

SPKのプログラム領域、ファイル名の参照表、データの参照表、隣接するサイズ値を
照合し、次の5ファイルをGit管理外へ取り出しました。Majiproの `cxd5610_EVB.cfg` ではなく、
この公式パッケージにはSpresense用 `spresense_i2c.cfg` が入っています。

| ファイル | サイズ（バイト） | 最大4086バイトで分割した転送フレーム数 |
| --- | ---: | ---: |
| `updater.efpk` | 30,592 | 8 |
| `cxd5610_sdk.efpk` | 719,040 | 176 |
| `cxd5610_app.efpk` | 258,032 | 64 |
| `cxd5610_lib.efpk` | 1,088 | 1 |
| `spresense_i2c.cfg` | 3,008 | 1 |

公開 `mkspk` と同じCLEFIA-CMAC計算でSPK内のチェック値との一致を確認しました。
これは公開パッケージ形式による整合性の確認であり、秘密鍵による署名認証ではありません。
各ファイルはSPKの対応領域とのSHA-256一致を確認し、合計250フレームについて
ヘッダー・ペイロードのチェックサム、通番、最大長をオフラインで検査しました。
分割したデータの再結合も元の全1,011,760バイトと一致しています。

ここまでのオフライン検査はCXD5610上でEFPKが受理・実行されたことを意味しません。
検査時点でモジュール上の版は `v00.103` でした。後続の実機試験とは区別します。

原版ZIP・SPK、`images/` の5ファイルに加え、次の検証記録をGit管理外に保存しています。

- `manifest.json`: 公式配布元、ZIP/SPKのサイズ・ハッシュ。
- `spk-metadata.json`: SPKのロード領域と範囲検査。
- `spk-integrity.json`: SPKのCMAC・フッターの検査結果。
- `embedded-image-audit.json`: ファイル名とデータの参照表、位置、サイズ、SHA-256。
- `update-plan.json`: 各ファイルの分割数、再結合検査、公開サンプルに基づくコマンド計画。

#### Piへの移植に向けた事前確認

GNSSへデータを送らず、試験用PiのI2Cアダプターの機能を照会しました。

| 項目 | 読み取った値 |
| --- | --- |
| 機種 | Raspberry Pi Zero 2 W Rev 1.0 |
| カーネル | `6.18.50+rpt-rpi-v8` |
| I2Cアダプター | `/dev/i2c-1`、`bcm2835 (i2c@7e804000)` |
| `I2C_FUNCS` | `0x0eff000d`、`I2C_FUNC_I2C` あり |
| Device Tree上のクロック | `400000 Hz`。変更していない |

この照会はアダプター機能の確認です。4091バイトの連続転送や、GNSS更新の成功を
実測したものではありません。
[LinuxのI2Cユーザー空間API](https://www.kernel.org/doc/html/latest/i2c/dev-interface.html)
には通常のI2C転送とSMBus転送があります。更新データはSMBusの32バイトブロックへ
分割せず、受信機が受理するI2Cトランザクションを保つ必要があります。
[Raspberry Piカーネルの `i2c-dev.c`](https://github.com/raspberrypi/linux/blob/rpi-6.18.y/drivers/i2c/i2c-dev.c#L151)
の `write()` 上限は8192バイトなので、更新データにヘッダーを加えた最大4096バイトもこの上限内です。
これはユーザー空間APIの上限であり、実機での転送保証ではありません。

移植時には、全入力の存在・サイズ・ハッシュを更新開始前に検査し、ファイルの読み残しや
短いI2C転送を失敗として扱います。過去のRTCM試験で発生したIRQが最初からHighの場合も扱い、
応答のコマンド番号・長さ・ステータスを照合します。
Sonyサンプルは終了時にRESETをLowにするため、Piでの終了処理は、更新後の版照会と
通常測位の確認ができるように設計し直す必要があります。

Sonyサンプルの100 kHzは実装上の設定であり、`v144` の更新に100 kHzが必須と
確認したものではありません。接続中のPiの400 kHz設定は変更していません。

<a id="i2c-firmware-update"></a>

### Pi向け更新ツールと実機試験（2026-10-16）

`examples/i2c_firmware_update.py` を追加し、試験用Piに接続したSpresense GNSSアドオンを
**`v00.103` から `v00.144` へI2C経由で更新しました**。
更新後のリセットと版照会まで正常終了し、所要時間は約32.1秒でした。
I2Cは400 kHz、アドレス `0x24`、実機で確認した `/dev/gpiochip0` のGPIO17を使用しました。
GPIO番号はこの実機の確認値であり、他のPiへそのまま適用しません。

実装はSony Majiproの公開更新手順をもとに、LinuxのI2CデバイスとPython版libgpiod v2へ移植しています。
版照会だけの `--query`、入力を検査するだけの既定動作、書き込みを行う `--write` を分けています。
入力は公式ZIP/SPKを直接指定する `--package`、または事前抽出済みの `--image-dir` です。
公式ZIP/SPK全体と5イメージのサイズ・SHA-256を検査し、全イメージをメモリーへ読み込んでから
デバイスを開きます。ZIP/SPKは実行せず、ZIP内のパスをファイルシステムへ展開しません。
ファームウェア自体はこのリポジトリへ含めません。

| 更新処理 | 実機の結果 |
| --- | --- |
| 通常測位停止 `0x31` | `00` |
| 更新モード移行 `0x00` / `ff` | `ea`（`-22`）。この試行では転送・書き込みを開始せず停止 |
| 消去して更新モードへ再起動 `0x00` / `00` | `00`。明示的に `--enter-mode erase` を指定した別の試行で実行 |
| 更新プログラムのRAM転送・実行 | 8フレームと `0x02` がすべて `00` |
| SDK / アプリ / ライブラリ / Spresense用設定の転送 | 176 / 64 / 1 / 1フレームがすべて `00` |
| 各イメージの書き込み `0x09` | 4回すべて `00` |
| リセット `0x00` / `01` | `01` |
| リセット後の版照会 `0x06` | `v00.144` |

`erase` はeMRAMを消去する方式です。`force` の拒否から自動で切り替える処理は入れていません。
公式Spresense更新アプリの解析では、RESETを操作し、起動直後にタイミングを変えながら
`0x00` / `ff` を送る処理を確認しました。今回の通常配線にはRESET接続がないため、
Sony公開実装にもある消去方式を使用しました。消去後の通信断や電源断からの復旧は実証していません。

最初はMajipro実装と同じヘッダー／データを分ける送信を試みましたが、
更新モード移行時にI2Cがタイムアウトし、SCLがLowのままになりました。
この時点で転送・書き込みは始まっていません。ユーザーによる電源再投入後、
既存Piドライバーと同じくヘッダーとデータを1回の書き込みにまとめて解消しました。
最大パケットは4096バイトです。I2Cコントローラーの一時的な再接続も試しましたが、
永続設定は変更していません。

IRQが初めからHighの場合を扱い、各応答のコマンド番号・サイズ・チェックサム・結果を照合します。
IRQを排他的に取得し、短い転送や負の応答で停止します。エラー後に自動再送・消去・リセットは行いません。
同じ `v00.144` が既に入っている場合は照会だけで終了します。
これらの停止条件、イメージ破損、転送順序、IRQ待ち、RTCM形式などのPythonテスト37件がホストとPiで通っています。
公式ZIPのオフライン読み込みも両方で確認しました。書き込んだeMRAM全体の読み戻し比較は行っていません。

以下は再実行用の例です。事前にSonyの使用許諾に従って公式ZIPを取得し、
Python版libgpiod v2を使える環境で実行します。保存先は毎回新しいGit管理外のディレクトリを指定します。

```sh
# Validate the complete package without opening any hardware device.
python3 examples/i2c_firmware_update.py \
  --package "/path/to/gnss_addon_fw_updater_v144.zip"

# Query only; select the GPIO chip and IRQ line for your board.
python3 examples/i2c_firmware_update.py --query \
  --gpio-chip /dev/gpiochip0 --irq-line 17

# This explicitly erases eMRAM before uploading the audited v144 images.
python3 examples/i2c_firmware_update.py --write --enter-mode erase \
  --package "/path/to/gnss_addon_fw_updater_v144.zip" \
  --gpio-chip /dev/gpiochip0 --irq-line 17 --expected-version v00.103
```

実際の書き込みでは抽出済みの同一イメージに `--image-dir` を使いました。
実行した引数は `update-erase-launch.json` と `update-erase/summary.json` に保存しています。
作業ファイルとログはGit管理外に保存しています。
`update/` は最初のI2Cタイムアウト、`update-single/` は強制更新拒否、
`update-erase/` は更新成功のログです。実際に書き込んだスクリプトも `update-erase-used.py` に保存しています。

#### 更新後の通常測位とRTCM出力

ユーザーの指示に従って通常測位の観測を5分で終了し、RTCMの確認へ進みました。

| 対象 | 通信 | Fix | 衛星通知のAPI値 | 初回Fix | 更新間隔 | エラー |
| --- | --- | --- | --- | --- | --- | --- |
| Spresense GNSS Add-on、`v00.144`、I2C | 成功。約3.22秒で最初の通知、298回の受信時刻更新 | 約35.02秒で3D。最終 `mode=3/status=2`。初回Fix後の新しい266スナップショットすべてでFix成立 | 最終 `39/58`、各最大 `42/60` | ドライバー初期化からの時間。コールドスタートTTFFの測定ではない | Fix後の受信時刻差は中央値1秒、最小1秒、最大約1.01秒 | 300.22秒の観測中に読み取り・例外・ドライバー標準エラーとも0件 |

`used_sats/total_sats` は追尾フラグ数／通知項目数です。同じ衛星の別信号を別項目として数えるため、
測位に使った衛星数や、重複を除いた衛星数ではありません。
座標は有効範囲と日本の範囲に収まり、Fix成立後の変動範囲の対角は約10.7 mでした。
測量済みの基準座標との比較ではありません。起動直後に受信機時刻が2000年から2026年へ確定する
大きな時刻補正が2回あり、集計の最大時刻差へ含まれます。通信の長時間停止とは区別します。
元は600秒で起動し、ユーザーによる短縮指示を受けて約300秒でSIGINT終了したため、
`summary.json` の `interrupted=true/completed=false` はこの予定変更を表します。

RTCMはSonyサンプルの `0x07102202`（送信バイト `02 22 10 07`）を使用しました。

| RTCMの確認 | 結果 |
| --- | --- |
| `0x45` の出力種類設定 | `00` |
| `0x34` に `80 81 82 83 84 89` を指定 | `00`。`v00.103` での `ea` と異なり受理 |
| 収集時間 | 約60.22秒 |
| `0x84` 通知／RTCM3フレーム | 165件／165フレーム、12,165バイト |
| 内訳 | `1077` GPS MSM7: 53、`1097` Galileo MSM7: 53、`1117` QZSS MSM7: 53、`1019` GPS暦: 4、`1044` QZSS暦: 2 |
| CRC24Q／宣言サイズ／余剰バイト | エラー・不一致・余剰すべて0 |
| I2C受信・設定コマンド | 読み取りエラー0、コマンド拒否0、標準エラー0バイト |
| 終了処理 | 測位停止、RTCM無効化、通常通知への復元がすべて `00` |

**v144のSpresense実機では、通知ペイロードは `80 + uint16_leサイズ + RTCM` の3バイトヘッダーでした。**
REYAX資料の5バイト形式にはフレーム総数と通番があり、そのまま適用するとRTCM先頭2バイトを落とします。
解析処理を両形式へ対応させ、元の生ログを変更せず再解析したところ165フレームすべてのCRCが一致しました。
当初の解析結果は `summary-initial-parser.json` に保存し、修正版の結果を `summary.json` に記録しています。
3バイト形式では通番が得られないため、CRC正常だけで丸ごとの通知欠落を否定するものではありません。

最初のRTCM試行は版照会のIRQ待ちで停止し、出力設定へ到達していませんでした。
通常ドライバー終了時の応答が残る場合と、ヘッダー読み取り後もIRQが一時的にHighのままの場合を
区別し、更新ツールの受信待ちを修正しました。最初の読み取りのみ既存Highを認め、
後続のデータには新しいエッジを待ちます。修正後の版照会とRTCM試行では `v00.144` を確認できました。

追加の20秒の通常測位確認でも3D Fixは成立しましたが、終了時に `stop during close failed: Resource temporarily unavailable`
が1件出ました。後続の照会では不完全な応答とI2Cタイムアウトが発生し、SCLがLowに保持されました。
Pi側のI2Cコントローラーを一時的に再接続しても解消せず、ユーザーによる2回目の電源再投入を行いました。
再投入後も `v00.144` を読み取れており、再書き込みは行っていません。

C側の `send_command()` が最初の応答ヘッダーをIRQ待ちなしで読み始め、
別コマンドの応答でも成功と判定できる処理だったため修正しました。
最初の既存Highと後続パートの新しいエッジを分けて待ち、各パートの読み取り前に古いイベントを除き、
要求したコマンド番号の応答だけを採用します。
`tests/test_i2c_transport.c` は、実際のC実装へ偽のI2C/GPIOを接続し、
準備前の読み取り、最初からHighのIRQ、残存する別コマンドのACK、応答が来ない場合の4シナリオを検査します。
Pi上で4件とも成功し、実機へアクセスせず検証しています。

```sh
cc -O2 -pthread tests/test_i2c_transport.c -o /tmp/test-i2c-transport -lgpiod -lm
/tmp/test-i2c-transport
```

修正したC拡張をPiの試験用仮想環境へ再インストールし、電源再投入後に短い再試験を行いました。

| 修正後の確認 | 結果 |
| --- | --- |
| 通常測位20秒 | 通信成功、17回の時刻更新、読み取り・例外・標準エラー0件。再投入後の短時間試験のためこの区間では未Fix |
| 通常ドライバーの終了 | 約0.244秒、STOP失敗の警告なし |
| 終了直後の版照会 | `v00.144`、成功 |
| RTCM約20.01秒 | 84通知／84フレーム、GPS・Galileo・QZSSのMSM7が各28、CRC・サイズ・余剰・受信エラー0件 |
| RTCM終了処理 | STOP、無効化、通常通知への復元がすべて成功 |
| 最後の版・衛星設定・配線状態照会 | `v00.144`、`00 ef 3f`、SDA/SCLともHigh |

再投入前の5分観測で成立した3D Fixと、再投入後の短時間試験の未Fixは別の観測結果です。
最終再試験のログは `normal-v144-safe-close/`、`query-after-safe-close/`、
`rtcm-v144-regression/`、`final-state-verified.json` に保存しています。

#### RTKLIBによるRINEX変換と搬送波位相

[RTKLIB 2.4.3 b34](https://github.com/tomojitakasu/RTKLIB/tree/180043ee24b6d2b168f98b64be15f69d50046b1a)
の `convbin` をmacOS上でビルドし、今回の60秒のRTCMをRINEX 3.02へ変換しました。
終了コード0、観測53エポック、航法暦6レコードです。

| 系統 | 非ゼロのL1搬送波位相 `L1C` | 非ゼロのL5搬送波位相 `L5Q` |
| --- | ---: | ---: |
| GPS | 263 | 40 |
| QZSS | 94 | 76 |

これは衛星・エポックごとの観測値の件数です。GPSの10衛星、QZSSの2衛星が観測ファイルに現れました。
GalileoのMSM7フレームは受信しましたが、今回の53件は衛星・信号セルが空で、Galileoの観測値はありません。
GPS/QZSSのMSM7信号IDは2（L1 C/A）と23（L5 Q）でした。

比較に使ったRTKLIB 2.4.2の既定出力項目では `L5X` が選ばれ、実際の `L5Q` が省かれていました。
同版では `-scan` を付けて再変換すると `L5Q` が出力されました。
最終成果物は自動で信号を検出する上記2.4.3での変換結果です。
ヘッダーにL5の項目があることだけで判断せず、実データの非ゼロ値を数えています。

ここで確認したのは、I2C経由のRTCM出力とL1/L5位相フィールドの数値抽出です。
後述の1時間観測ではL5は全件ロック時間0・半周期未解決だったため、
非ゼロの数値が得られたことだけで、有効なL5搬送波の連続追尾を確認したとは判断できません。
ロック・半サイクル・サイクルスリップを含む観測品質の評価、長時間の観測、
同時間帯の基準局データとのPPK／RTK解算は次の検証です。RTKの整数解が得られたという報告ではありません。

追加の衛星選択照会 `0x33` は `00 ef 3f` を返しました。
既存Cソースの固定値 `f7 3f` を独立した試験で指定すると `ea` で拒否されました。
予約ビット4を0にし、QZSS L1を含む `ef 3f` へCドライバーの初期設定を修正しました。
実機への `0x32` / `ef 3f` は `00`、再照会は `00 ef 3f` を返しました。
この設定変更ではバックアップ保存を行っていません。

主な実行コマンドは以下です。今回のGPIOチップ番号は実機で確認した値です。

```sh
python examples/i2c_window_test.py --duration 600 --interval 0.2 \
  --board-model 'Spresense GNSS Add-on Board' --log-dir <private>/normal-v144
# Observation was ended after 300 seconds at the user's request.

python3 examples/i2c_rtcm_test.py --gpio-chip /dev/gpiochip0 --irq-line 17 \
  --duration 60 --rtcm-mask 0x07102202 --log-dir <private>/rtcm-v144-sony-fresh

convbin -r rtcm3 -v 3.02 -f 3 -od -os -tr 2026/10/15 23:37:00 \
  -o rover.obs -n rover.nav observations.rtcm3
```

実際のPythonインタープリターと全引数は `normal-v144-launch.json`、
`rtcm-v144-sony-fresh-launch.json`、`rinex-2.4.3/conversion.json` に保存しています。
Pi上のGit管理外の保存先で、`normal-v144/` に通常測位、
`rtcm-v144-sony/` に照会タイムアウト、`rtcm-v144-sony-fresh/` に成功した生ログ・修正後解析、
その下の `rinex-2.4.3/` に最終RINEXを保存しています。位置を含むデータはGitへ追加しません。

### RTCM対応版を入手できるか

| 対象 | 確認できた根拠 | Spresenseアドオン用ファームウェアの入手状況 |
| --- | --- | --- |
| 通常測位・QZQSM | Sony SDKはQZQSMに `v00.144` 以降を要求 | 公式 `v144` アップデーターの配布あり |
| RTCM補正入力によるDGNSS | Swift公式のCXD5610GF EVB手順は `v00.151` 以降を指定 | アドオン用の公開配布先・互換性は未確認 |
| RTCM MSM7観測データ出力・位相フィールド | 今回の実機 `v00.144` でRTCM受信とL1/L5位相フィールドを確認。有効な連続追尾は別に評価が必要 | 公式Spresense用 `v144` から更新可能。すべての機器・版・出力種類の互換性まで保証するものではない |

[Swift公式「Using Skylark Dx with a Sony CXD5610GF receiver」](https://support.swiftnav.com/support/solutions/articles/44002531395-using-skylark-dx-with-a-sony-cxd5610gf-receiver)
（2025-06-03更新）は `v00.151` 以降を確認し、Skylark Dxの `DX-MSM1` 補正を受信機へ
入力する手順です。確認結果はGGAの品質値 `2`（DGPS）です。
同資料はSonyへの申請によりGNSS MonitorとApplication Noteを入手するよう案内していますが、
ファームウェア自体の配布先は記載していません。RTKLIBの `str2str` はNTRIPクライアント／
シリアル中継として使われており、搬送波位相を使ったRTKやPPKの実証ではありません。

公開資料だけから `v144` のRTCM出力を断定はできませんでしたが、今回の実機試験で
`v00.144` のI2C出力を確認できました。`v00.151` の対応状況は今回の試験対象ではありません。
`v00.151` は同EVB手順の必要条件であり、RTCM観測データ出力の対応開始版ではありません。

[SonyのGNSS製品問い合わせ窓口](https://info.sony-semicon.com/jp/inquiry)へ確認する場合は、
次の条件を明示すると、補正入力と観測データ出力の取り違えを避けられます。

- 機器はSpresense GNSS Add-on Board（CXD5602PWBGNSS1W / CXD5610GF）、現状 `v00.144`。
- 目的はI2C経由のMSM4/MSM7観測データ出力、特にL1/L5の搬送波位相をRTKLIBで使用すること。
- 旧 `v00.103` では `0x84` 通知指定が `-22`。今回の `v00.144` では受信できた。
- 公開 `v144` の対応可否、必要な別ビルド／最低版、アドオン適合SPKまたはEFPKの提供可否、
  使用許諾・NDA・費用などの条件、I2C通知手順、復旧・旧版への戻し方を確認したい。

ユーザー確認後にEULAへ同意し、公式ファームウェアの取得・検査と、
許可された実機への `v00.144` 書き込みを実施しました。問い合わせの送信は行っていません。

## 長時間観測とPPKの検証手順

通常測位のFix、RTCMの通信成功、搬送波位相の追尾、PPKの整数Fixは別々に判定します。
RINEXに非ゼロの `L1C` / `L5Q` が存在しても、連続した有効な搬送波位相を取得できたとは限りません。
MSM7のロック時間、半周期未解決フラグ、C/N0、時刻の連続性を併せて確認します。
特に、共有の粗距離からRINEXの数値を生成できても、個々の信号が追尾中とは限りません。

1時間の観測には、既存のプローブを時間指定して使用できます。
GPIOチップとIRQは実機で確認した値に置き換えてください。同じ機器へ並行してアクセスしません。

```sh
python3 examples/i2c_rtcm_test.py --gpio-chip /dev/gpiochip0 --irq-line 17 \
  --duration 3600 --rtcm-mask 0x07102202 --log-dir <private>/capture
```

### 基準局データ

[国土地理院の電子基準点データ](https://terras.gsi.go.jp/data_download.php)では、
30秒間隔の観測値と放送暦を取得できます。衛星系 `GRJE`、RINEX 3.02を選択すると、
GPS・GLONASS・QZSS・Galileoを含むデータをRTKLIBで扱えます。
公開には時間差があるため、取得できたファイルの最初・最後の観測時刻を読み取り、
受信機データとの実際の重複区間を使用します。ファイル名の時刻だけで判定せず、
RINEXヘッダーの時系も確認してください。

基準局座標はRINEXの `APPROX POSITION XYZ` と、使用した座標の出典・時点・座標系を記録します。
[日々の座標値 F5.1](https://terras.gsi.go.jp/pos_main.php)はITRF2020に準拠し、
観測当日の座標が直ちに公開されるものではありません。
古い概略座標と日々の座標を混ぜると、受信機の測位誤差と座標系・時点の差を取り違えることがあります。
座標を変えて比較する場合も元の結果を残します。

[電子基準点PCV補正データ](https://terras.gsi.go.jp/pcv_top.php)は、
アンテナ機種と架台の組合せをRINEXヘッダーに合わせて選択します。
国土地理院の案内に従い、電子基準点のアンテナ高は0とし、アンテナ定数を重ねて適用しません。
公開NGS形式のL1/L2補正にL5の校正が含まれるとは限りません。
RTKLIB 2.4.3はL5の補正がない場合にL2を代用するため、L1を主とする検証と
L1/L5の追加検証で適用条件を明記します。受信機側アンテナの型番・校正が不明なら、
絶対的なcm精度を確認したとは報告しません。

### RTKLIB変換時の確認

上記の2.4.3 b34では、入力の走査後に先頭の空のMSMを処理すると、走査終了時の観測が
残り、途中の時刻が出力されなくなる場合があります。
生データを保存し、最初の非空MSMのGPSTを `convbin -ts` に指定すると回避できました。
変換後は、非空のMSMのユニークな時刻数とRINEXのエポック数が一致することを確認します。

同版の `src/rtcm3.c` のMSMデコーダーには、次の演算があります。

```c
lossoflock(rtcm,sat,idx[k],lock[j])+(half[j]?3:0);
```

ロック喪失と半周期未解決が同時に発生すると `1 + 3 = 4` となり、
半周期未解決のビットが消えます。検証用の変換器では加算をビットORへ置き換え、
保守的な既存のフラグを保持しました。

```c
lossoflock(rtcm,sat,idx[k],lock[j])|(half[j]?3:0);
```

元の変換器、変更差分、ビルド条件、バイナリーのハッシュ、合成MSMによる4通りの検査を保存します。
変更前後で、位相・擬似距離・ドップラー・C/N0の数値が変わっていないことも照合します。
PPKのFixを得るために、ロック喪失や半周期未解決のフラグを削除してはいけません。

### 解算と判定

30秒の基準局データには、受信機の全時刻のRINEXを入力して `rnx2rtkp -ti 30` で
同時刻の観測を選びます。途中のスリップ情報を保存するため、独自処理で行だけを間引きません。
基準局との最大時刻差を制限し、長い時間差の位相をそのまま同時刻として扱わないようにします。
RTKLIBの `-f 2` はGPS/QZSSではL1+L2です。L1/L5を含めるには `-f 3` が必要です。

固定設置ではStaticとKinematicを区別し、前向き・後向き・Combined、
前半・後半の一致、Fixed/Floatの件数、使用衛星数、基準局との時刻差、残差を記録します。
通常の整数解比のしきい値3.0を使う場合も、比がしきい値を超えたことだけで
正しい整数解と判断せず、方向を変えた解や別の基準局との一致を確認します。
Staticの拘束で変動が小さくなったことと、絶対位置が正しいことも区別します。

設定ファイルでは `pos1-posmode=static` のように `=` の直後へ値を書きます。
同版は列挙値の先頭の空白を除去せず、不正な値でも処理を続行するため、
終了コードだけでなく、警告と解ファイルの実際の設定ヘッダーも確認します。

観測生データ、RINEX、基準局データ、座標値、設定、コマンド、解算結果はすべてGit管理外へ保存します。

### 1時間の実機観測とPPKの結果（2026-10-16）

同じSpresense GNSS Add-on Board / `v00.144` で、現在の設置条件のまま
約3,600.08秒のRTCM観測を実施しました。

| 項目 | 結果 |
| --- | --- |
| I2C通信・RTCM | 11,269通知／フレーム。CRC、サイズ、余剰バイト、読み取りエラーはすべて0 |
| 通常測位 | 最初の有効な時刻通知から約20.99秒でFix。その後の3,577通知すべてで3D。コールドスタートTTFFではない |
| 通常測位の更新 | 3,598回の時刻通知、中央値1秒、最小0.99秒、最大1.01秒。時刻の飛びは0 |
| 衛星数 | 通常通知の重複を除いた追尾は最終26、最大27衛星。QZSS L1Sのサービス信号を除外。信号項目数とは区別 |
| RINEX | 3,607エポック。先頭の空のMSM以外のユニークな時刻数と一致。GPS、Galileo、QZSSのL1/L5フィールドを出力 |
| 有効な位相追尾 | L1のロック時間が正で半周期未解決でない衛星は中央値5、最大7。L5は全件ロック時間0・半周期未解決 |
| 終了処理 | STOP、RTCM無効化、通常通知への復元がすべて成功。終了後の版照会も `v00.144`、SDA/SCLはHigh、IRQは解放 |

別の近隣電子基準点2局から、同じ時間帯の30秒間隔RINEX 3.02と放送暦を取得しました。
座標には最新公開のF5.1（ITRF2020、2026-09-19時点）を使用し、
アンテナ・架台に一致する国土地理院PCVモデルを適用しました。
基準局データの公開待ちと、一度の公開直後のダウンロード失敗がありましたが、
再取得したファイルの120エポックと期間を検証してから解析へ使用しました。

主解析は初期5分を除いた50分間です。30秒で対応する101時刻があり、
受信機と基準局の時刻差は2〜3msでした。単独測位のカイ二乗検査に通らない20時刻があり、
主解析で出力された相対解は81時刻です。これは通信による観測欠落とは区別します。

| 解析 | 結果と判定 |
| --- | --- |
| Static L1、Combined、整数解比しきい値3.0 | 81解中5件がRTKLIBのFixed表示。ただし、前後方向で同時にFixedの時刻は0 |
| 前向き・後向きStaticの比較 | 同時刻の位置差は中央値約4.96m。両方向の最終推定同士も約5.69m差 |
| 前半・後半を独立に処理 | 最終推定の差は約5.87m |
| 別基準局との比較 | 前向きStaticの最終推定は約5.6cm差。同時にFixedの2時刻は約2.3〜2.9cm差。ただし、方向・時間帯による不一致を解消するものではない |
| C/N0を30dB-Hz以上に限定し、RAIM/FDEを有効化 | Combinedで93解中9件がFixed表示。両方向で同時にFixedの1時刻でも約1.54m差 |
| RTKLIB標準の基準局残差補間、受信機1Hz | 基準局記録の範囲内で追加確認。Combinedの2,440解中40件がFixed表示。両方向で同時にFixedの1時刻でも約3.96m差 |

**今回の設置条件では、安定したPPK整数Fixを確認できませんでした。**
少数のFixed表示や大きな整数解比だけを根拠に、cm測位に成功したとは判断しません。
L5の有効な追尾がなく、L1にもロックが不安定な観測が多数ありました。
アンテナの対応帯域、設置条件、観測値の品質が次の切り分け点です。
受信機側アンテナの校正と、測量済みの受信地点座標も未確認であり、絶対精度の実証ではありません。

対照として電子基準点同士のL1/L2解析も行い、前後方向の両方がFixedの45時刻では
位置差が中央値約2.2cm、最大約4.0cmでした。
RTKLIB自体の整数解算が一律に動作しない状態とは区別できています。
変換器の修正前後の全3,607エポックで、位相・擬似距離・ドップラー・C/N0の数値差は0でした。
位相の品質フラグは保持し、整数Fixを得るための観測値編集は行っていません。

実行コマンド、設定、基準局の出典、フレーム原本、RINEX、解算結果と品質グラフはGit管理外に保存しました。
観測場所に結び付く基準局名と座標の記録もGit管理外に保存しました。

## 次の比較対象

RYS8839のUART接続で `@VER` と `@RTCM` を確認し、
ファームウェア版とRTCMの取得可否をSpresenseの結果と比較します。
Spresenseの `v00.144` では1時間の観測と、同時間帯の基準局データを使ったPPKまで実行しました。
現状は安定した整数Fixが未確認のため、アンテナと受信環境を確認して位相品質を改善する必要があります。
