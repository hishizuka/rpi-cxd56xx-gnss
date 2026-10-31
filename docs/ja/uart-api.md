# POSIX UART Python API

[English](../uart-api.md) | 日本語

[README](../../README.ja.md) · [UART配線・衛星マスク](uart.md)

`CXD56xxUART`で、1本のPOSIXシリアルポートに混在するNMEA・RTCM3・Sonyバイナリを読み取れます。
既存I2C用の`CXD56xx`とは別のクラスです。`cxd56xx-gnss` CLIは引き続きI2C用です。
Python 3.13以上とpynmeagpsが必要です。

## このチェックアウトからのインストール

```sh
CXD56XX_BUILD_I2C=0 python -m pip install '.[uart]'
```

I2C拡張やlibgpiodをビルドしないUART専用wheelを作ります。
Linuxで両APIを利用する場合は環境変数を省略して`.[uart]`をインストールしてください。
UART専用インストールでは`CXD56xx`とI2C CLIを利用できません。

## 起動と受信

```python
import time
from cxd56xx_gnss import CXD56xxUART

with CXD56xxUART('/dev/serial0', baudrate=115200, rtscts=False) as gps:
    gps.start()
    try:
        while True:
            if gps.peek() == 0:
                print(gps.position)
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
```

コンストラクターはUARTを開いて受信を始めます。既定では受信機へのコマンドを送りません。
`qzss_dcr_enabled`を明示した場合は、後述の同期DCR設定まで実行します。
実際のボーレートとハードウェアフロー制御を指定してください。
ホスト側を8N1に設定し、HUPCLを解除します。受信待ちデータの破棄、受信機側の速度変更、
GPIO操作、リセット、FW更新は行いません。ファイルロックで同ライブラリ同士を排他しますが、
Android HALなどロックに従わないプロセスは排除できません。先に他のUART所有者を停止してください。
所有者の切り替えは呼び出し側の責務です。

`start()`はWUP（最大10回、各2.5秒）→ GSTP → GSRの順に送信します。
測位中のGSRがbusyになることを避けるため、Idleを確認してからホットスタートします。
`stop()`はWUP → GSTPです。`get_satellite_mask()`はWUP後にGGNSで照会します。
`set_l1s_enabled(bool)`はWUP → Idle → GGNS照会 → bit 5だけ変更 → 再照会 → GSRです。
他の衛星設定は維持します。**L1S追尾の設定とDCR出力の設定は別です。**

`close()`は実行中のコマンドと受信スレッドの終了を待ち、UARTを閉じます。
受信機には何も送らないので、測位やRTCM出力は継続し得ます。
必要に応じて`stop()`や`set_rtcm_mask(0)`を明示的に呼んでください。
これらは同期APIです。非同期アプリからは`asyncio.to_thread`で呼び出します。

## 位置データの定義

`peek()`は新しいRMC時刻のデータがあれば0、なければ`-errno.EAGAIN`を返します。
0のとき`position`の不変な`UARTPosition`を更新します。同名の個別プロパティも利用できます。
未取得値は`None`です。

| フィールド | 出典・単位 |
| --- | --- |
| `lat`, `lon` | 有効なRMC、度 |
| `alt` | 同時刻GGAの平均海面からの高度、m |
| `geoid_separation` | 同時刻GGAのジオイド高、m。高度とは別に保持 |
| `speed` | 有効RMCのノット × 1852 / 3600、m/s |
| `track` | 有効RMCの対地進行方向、真北基準の度 |
| `timestamp` | 有効RMCの日時をUTC Unix秒へ変換 |
| `mode` | 同じ周期内のGSA navModeの最大値1/2/3。有効RMCでGSAがなければ`None`、無効RMCなら1 |
| `status` | GGAのqualityコード。I2Cのバイナリstatus列挙値とは別 |
| `pdop`, `vdop` | 同じ周期の最後のGSA、無次元 |
| `hdop` | 同時刻GGA、無次元 |
| `used_sats` | 同時刻GGAの使用衛星数 |
| `total_sats` | 完結したGSVの衛星IDを星座内で信号間重複排除した総数 |

GGAからRMCまでを1周期として収集します。GGAとRMCの時刻が不一致なら、そのGGAとGSA/GSVは使いません。
RMCが無効なら緯度経度・高度・速度・方位・timestampを消去します。
GSA/GSVには時刻がないため、この結合は受信機の出力順序に依存します。
GSVの分割出力が未完結、またはGNと星座別talkerが混在する場合の総衛星数は`None`です。
epx/epy/epvや水平精度の推定は追加しません。
I2Cバイナリ高度の基準面は、このUART対応では確定していません。高度の基準が同じとは扱わないでください。

## 搬送波位相を利用するための生RTCM

```python
import time
from cxd56xx_gnss import CXD56xxUART

with CXD56xxUART('/dev/serial0') as gps:
    gps.set_rtcm_mask(0x07102202)
    try:
        while True:
            for frame in gps.read_rtcm():
                print(frame.message_type, frame.data, frame.received_at)
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        gps.set_rtcm_mask(0)
```

`set_rtcm_mask(mask)`はWUP → Idle → `@RTCM <16進マスク>` → GSRで設定します。
`0x2`はGPS MSM7、`0x07102202`はGPS/Galileo/QZSSのMSM7とエフェメリスです。
上記はv00.201のUART向け設定です。対応内容はFWに依存します。
設定は揮発性で、保存・CFG変更コマンドは送りません。変更前のRTCMマスクを照会するAPIはありません。

`read_rtcm()`はCRC24Q確認済みの`RTCMFrame`を取り出します。
`message_type`、ヘッダー・本文・CRCを含む原文`data`、ホストの受信Unix秒`received_at`を保持します。
FIXがなくても受信できます。別途pyrtcmを導入すれば解析できますが、ライブラリの必須依存ではありません。

CRCが正しくても搬送波位相が有効とは限りません。MSM7の欠測コード、ロック時間、半周期フラグは別途判断が必要です。
原文を維持し、有効ADRや測位精度への換算は行いません。

## DCRの受信・設定

I2Cと同じ`set_qzss_dcr_enabled(bool)`、`read_dcreports()`、`dcreports_dropped`を提供します。
`qzss_dcr_enabled=True/False`をコンストラクターへ明示して初期設定もできます。
既定の`None`では従来どおり受信機へコマンドを送らず、流れてくるQZQSMを受信します。

```python
import time
from cxd56xx_gnss import CXD56xxUART

# Match the receiver's actual baud rate and flow-control configuration.
with CXD56xxUART(
    '/dev/serial0', baudrate=4_000_000, rtscts=True, qzss_dcr_enabled=True
) as gps:
    try:
        while True:
            for report in gps.read_dcreports():
                packet = report.to_azarashi_packet(reconstruct_crc=True)
                print(report.satellite_id, report.received_at, packet)
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        gps.set_qzss_dcr_enabled(False)
```

有効化はWUP → GSTP → GGNS → GNSのbit 5をON → GGNS照合 →
`BSSL 0x0E0F7FEF` → GSRの順です。他の衛星設定ビットを維持し、FW/CFG/保存設定は変更しません。
これはv00.201のASCII UARTのQZQSMに対応する出力マスクです。多くの診断電文も出るため、
通常位置情報だけの場合より通信量が増えます。全FWでの対応や必要最小ビット構成は未確定です。
上の例は4,000,000bps・RTS/CTS接続です。115200bpsでは診断出力が帯域を超える
可能性があります。例は受信機が記載の通信設定で動作していることを前提とし、受信機の速度は変更しません。
L1S追尾の設定が受理されても、衛星の捕捉と本文の到着を待つ必要があります。

無効化はL1Sのbit 5をOFFにし、BSSLを文書既定`0xEF`へ設定して再開します。
変更前のNMEAマスクを照会するAPIはないため、元のマスクへの復元ではありません。
FWによって診断電文が残る場合もあるため、全出力構成の復元が必要なら呼出し側で管理します。
`close()`は設定を変更しません。追尾だけを操作する`set_l1s_enabled()`を後から使うと、
DCR設定の確定状態を取り消すため、本文受信には再度`set_qzss_dcr_enabled()`を使用します。

戻り値はI2Cと同じ`DCReport(satellite_id, data, received_at)`です。
衛星IDと電文のビットを保持し、QZQSMの63桁HEXの末尾にパディングを補って32バイトへ揃えます。
`received_at`はホスト受信時のUnix秒です。本文CRCは検査・排除・自動補完せず、
必要な場合だけ上の例の共通APIでコピーのCRCを補完します。これは受信内容の検証ではありません。
測位FIXや`peek()`とは独立して本文を取り出せます。

変更中・明示的な無効化中は本文をキューへ入れず、変更前の未読本文も破棄します。
`session_status.qzss_dcr_requested`が要求値、`qzss_dcr_applied`がACKまで確認した設定です。
未設定・不確定は`None`です。`True`でも衛星捕捉や本文受信済みを意味しません。
途中の拒否・ACK不着・再照会不一致は不確定な設定を使用しないようセッションを失敗状態にし、
`CXD56xxSessionError`を送出します。UARTのDCR変更では、不明な元のBSSL値への自動復元を試みません。

`read_frames()`は他のキューとは独立に`UARTFrame(protocol, message_type, data, received_at)`を取り出します。
protocolはnmea/rtcm/binary/textです。NMEA XOR、RTCM CRC24Q、Sonyバイナリのヘッダー・本文加算チェックを確認します。
通常テキストには通信上のチェックサムはありません。Sonyバイナリは0x8Bも含め原文として保持し、
通知設定やバイナリDCRの本文解析は行いません。

## エラー・キュー

受信機が明示的に拒否した場合は`UARTCommandError`の`command`と`response`で確認できます。
通常の単独コマンド拒否ではセッションを継続できますが、測位が停止している場合があります。
DCR設定の途中の拒否は、前述のとおりセッション失敗になります。
`start()`等の適切な操作で明示的に復旧してください。
ACK不着、読み書き失敗、曖昧な衛星マスク再照会ではセッションを失敗状態にし、以後`CXD56xxSessionError`を送出します。
原因を解消してからclose・再生成が必要です。自動再試行は冪等なWUPだけです。再生成で受信機設定はリセットされません。

`session_status`はready/configuring/failed/closed、最後にACKを得たRTCMマスク、最後に照会した衛星マスク、失敗理由を保持します。
readyはUARTが開いている意味で、測位開始やFIXを保証しません。未取得のマスクは`None`です。

受信キュー3種は独立し、それぞれ`queue_size`件（既定256件）までです。
超過時は最古の要素を破棄し、`dropped`でframes/rtcm/dcreports別に件数を確認できます。
I2Cと共通の`dcreports_dropped`でも本文キューの破棄数を取得できます。
`parse_errors`はチェックサム・解析エラー数です。記録時は定期的にキューを読み出してください。
RTCM・バイナリ本文中のACKらしい文字列を、コマンド完了として扱いません。
