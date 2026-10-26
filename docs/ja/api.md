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
    gps.close()
```

実行用のサンプルは [examples/i2c_read.py](../../examples/i2c_read.py) にあります。
ダウンロードするか、リポジトリを取得し、仮想環境を有効にした状態で `python examples/i2c_read.py` を実行してください。

`close()` は測位停止の応答を待ってI²CとIRQを解放します。繰り返し呼んでも安全です。
停止に失敗した場合は `OSError`、解放後の `peek()` では `ValueError` が発生します。
デストラクターでも終了処理を試みますが、終了結果を確認するには `finally` で `close()` を呼んでください。

`peek()` は待機せずに戻ります。スナップショットの取得に成功すると `0`、最初の通知を待っている間は
`-errno.EAGAIN`、その他の通常エラー時は負のerrnoを返します。
復旧不能なセッション障害は `CXD56xxSessionError` です。プロパティは成功後に読み取ってください。
新しい通知がない場合は前回のスナップショットを返すため、更新の有無は `timestamp` を比較して判定します。

受信成功は、必ずしも測位成立を意味しません。`mode=3` / `status=1` は 3D 測位を示します。
`mode=2` は2D測位、`status=2` はDifferential GPSの有効なFixです。

| プロパティ | 意味 |
| --- | --- |
| `lat`, `lon` | 緯度・経度（度） |
| `alt` | 受信した高度（m）。追加のジオイド補正は行わない |
| `speed` | 速度（m/s） |
| `track` | 方位（度） |
| `mode`, `status` | 測位状態 |
| `pdop`, `hdop`, `vdop` | 精度低下率（DOP） |
| `used_sats`, `total_sats` | 追尾フラグ数／通知項目数。同じ衛星の別信号を含み、重複を除いた衛星数ではない |
| `timestamp` | UTC の Unix 時刻（秒） |

欠損値は `None` です。初期化時にデバイスを開けない場合は `OSError` が発生します。
I2CドライバーはQZSS L1S以外の衛星選択に固定設定を使います。プロトコルの違いは
[UART ガイドの I2C 衛星設定](uart.md#i2c-satellite-settings)を参照してください。
RTCM観測データの取得手順と短時間の実験用プローブは、[I2C の RTCM 収集ガイド](rtcm.md)を参照してください。
Piからのv144更新ツールの対象・依存関係・実行手順は、[ファームウェア更新ガイド](firmware-update.md)を参照してください。

## 設定の適用確認とセッション障害

コンストラクターは非同期で起動処理を開始します。
`wait_ready(timeout=15.0)` は衛星設定・通知設定・STARTの正常ACKを待ち、成功時に `True` を返します。
Fixは不要です。各コマンドの応答待ちは最大3秒、起動・自動復旧はそれぞれ1回試みます。
STOPだけは従来のquiet-start互換として `ETIMEDOUT` / `EAGAIN` を許容します。
設定とSTARTのACK欠落は許容せず、受信スレッドを終了します。

```python
import asyncio
from cxd56xx_gnss import CXD56xx, CXD56xxSessionError

gps = await asyncio.to_thread(CXD56xx, qzss_dcr_enabled=False)
try:
    await asyncio.to_thread(gps.wait_ready, 15.0)
    print(gps.session_status)
finally:
    await asyncio.to_thread(gps.close)
```

`wait_ready()` 自身の待機期限だけが先に切れた場合は `TimeoutError` です。
受信スレッドは継続するため、`session_status` を確認し、必要に応じて再度待てます。
待機はmonotonic時計を基準とし、ホストのUTC補正で期限が延びません。
期限はPythonメソッドの開始時に決め、内部ロックの取得待ちとC層の完了待ちで共有します。
ロック待ちで消費した時間を、取得後に再び追加しません。`timeout=0` はロック取得も待たず、
即時に取得できて設定確認済みなら `True`、待機が必要なら `TimeoutError` です。
C層へは残り時間をミリ秒単位で切り捨てて渡すため、1ms未満の残り時間は状態確認のみになります。
期限切れでも別の実行中の設定変更・受信スレッドは中断しません。

`session_status` は変更不可の `SessionStatus` です。

| フィールド | 意味 |
| --- | --- |
| `state` | `starting` / `ready` / `configuring` / `recovering` / `failed`。`ready` は設定確認済みで、Fix成立とは別 |
| `qzss_dcr_requested` | 維持する要求値。切り替え待ち・処理中は新しい要求値、復元成功後は元の値 |
| `qzss_dcr_applied` | 確認済みの値。起動・復旧・切り替え中と障害後は `None` |
| `error_errno`, `error_stage` | 復旧不能な障害の正のerrnoと段階（`startup` / `recovery` / `restore` / `transport` / `worker`）。障害なしは `None` |
| `change_errno`, `restore_errno` | 直近の切り替えと復元の失敗理由。正のerrno、失敗なしは `None` |

以前の設定へ復元できた切り替え失敗は通常の `OSError` です。
復元失敗・起動/復旧失敗・復旧不能な受信停止は、公開例外 `CXD56xxSessionError(OSError)` で区別します。
例外の `errno` は終端障害の理由、`stage` は発生段階、`change_errno` / `restore_errno` は両処理の理由です。
この例外を通常の `OSError` より先に捕捉してください。

障害後の `peek()`、`read_dcreports()`、`wait_ready()`、設定切り替えも同じセッション障害を通知します。
残存通報・測位スナップショットを無効化し、`EAGAIN` や空リストとして待機させません。
`session_status` と統計の `dcreports_dropped` は障害後も参照できます。
再開には明示的なcloseと再生成が必要です。closeが `OSError` を返した場合もリソースは解放済みです。
close後の読み出し・設定・状態・統計・完了待ちは `ValueError` になります。

Pythonの設定変更・完了待ち・closeは直列化します。
`asyncio.to_thread()` のawaitをキャンセルしてもC処理は継続しますが、closeはその処理の完了を待ってから解放します。
アプリ側でも処理状態とタスクの終了を管理し、Qtイベントループ上で同期メソッドを直接呼ばないでください。

## QZSS 災危通報（I2C）

`CXD56xx(qzss_dcr_enabled=True)` は起動時にQZSS L1Sと災危通報のバイナリー通知
（`0x8b`）を有効にします。`False` を指定すると無効で起動します。
`set_qzss_dcr_enabled(enabled)` で、測位を止めずに稼働中も切り替えられます。
設定は受信スレッドが受信機へ送り、両コマンドの正常応答を確認すると `True` を返します。
同期処理のため、asyncioからは次のように呼び出してください。

```python
import asyncio

gps = CXD56xx(qzss_dcr_enabled=False)
await asyncio.to_thread(gps.wait_ready)
await asyncio.to_thread(gps.set_qzss_dcr_enabled, True)
await asyncio.to_thread(gps.set_qzss_dcr_enabled, False)
```

最後に成功した設定はセッション復旧後も維持します。測位用QZSS L1-C/A・L5は変更しません。
切り替え成功時に未処理の通報を破棄し、OFF中・設定処理中は新しい通報をキューへ追加しません。
切り替え失敗時は `OSError` となり、以前の設定へ戻す処理を試みます。
復元も失敗した場合は `CXD56xxSessionError` です。インスタンスをcloseして再生成してください。
起動設定は従来どおり非同期で行います。コンストラクターの終了は受信機の設定完了を意味しません。

`read_dcreports()` は待機せず、未処理の通報を
`list[DCReport]` として取り出します。未受信時は `[]` です。
測位成立や `peek()` の呼び出しとは独立しています。

`DCReport` は `cxd56xx_gnss` からインポートできる、変更不可の名前付きタプルです。

| フィールド | 意味 |
| --- | --- |
| `satellite_id` | QZQSMの衛星ID。例：PRN 183は `55`。Sonyの通知内のIDをそのまま使う |
| `data` | L1Sメッセージ32バイトの独立した `bytes` コピー。受信した並びを保持し、250ビットの本体と後続のパディングを含む |
| `received_at` | ホスト時計による受信時刻。小数秒付きのUTC Unix時刻で、GNSSの `timestamp` とは独立 |

キューは受信順に64件保持し、読み出した通報は消費されます。同じ内容の再送も保持し、
重複排除はアプリ側に任せます。満杯になると最も古い未処理通報を破棄します。
`dcreports_dropped` は、そのインスタンスでキューがあふれて破棄した累計件数です。
参照しても通報は消費されず、読み出しでもリセットされません。
設定切り替えによる破棄は、キューあふれの累計に含みません。
`close()` 後は、読み出し・統計・切り替えAPIで `ValueError` が発生します。

受信処理はパディングを含む32バイトを変更せず、文字列化や通報内容の解析を行いません。
I2Cパケットのチェックサムは確認しますが、L1SのCRCと内容の検証はデコーダーの責務です。
受信機が出力したMT43・MT44はどちらも渡します。
試験した `v00.201` では両方の実受信を確認しましたが、採取した本文は
azarashi 0.17.1がL1S CRC不一致で拒否しました。正常な通報として受理できたことは意味しません。

### `DCReport.to_azarashi_packet(*, reconstruct_crc=False) -> bytes`

衛星ID 1バイト＋本文32バイトの`net`形式を返します。受信機へのアクセスやazarashiへの依存はありません。
既定の`False`では元の32バイトをそのまま使います。
`True`では本文の先頭226ビットからCRC24Qを再計算し、コピーのCRC領域24ビットだけを置き換えます。
元の`message.data`・受信時刻・本文226ビット・末尾6ビットは保持します。

この明示的な回避策でCXD5610の出力をazarashiへ渡せます。補完は受信CRCの検証ではありません。
元の受信値が必要な記録には`message.data`を使ってください。
`reconstruct_crc`がbool以外なら`TypeError`、手動生成した通報のID・データ長などが不正なら`ValueError`です。

```python
packet = message.to_azarashi_packet(reconstruct_crc=True)
report = azarashi.decode(
    packet, "net",
    timestamp=datetime.fromtimestamp(message.received_at, timezone.utc),
)
```

CRC計算と33バイト化はこのライブラリ側で行い、使用アプリは変換結果を既存のazarashi処理へ渡します。

[azarashiのAPI](https://github.com/nbtk/azarashi/blob/main/docs/api.md)には、
衛星ID 1バイト＋データ32バイトを `net` 形式で直接渡せます。
UDP通信やQZQSMへの変換は不要です。以下の例では、別途 `azarashi>=0.17.1` をインストールします。
公開済み0.17.1では形式の引数名が `msg_type`、上流のAPI文書では `msg_format` のため、
例ではどちらでも使える位置引数で `"net"` を指定しています。

```python
from datetime import datetime, timezone

import azarashi

# Use the existing CXD56xx instance; one worker owns the I2C receiver.
for message in gps.read_dcreports():
    try:
        report = azarashi.decode(
            message.to_azarashi_packet(),
            "net",
            timestamp=datetime.fromtimestamp(message.received_at, timezone.utc),
        )
    except azarashi.AzarashiReadOn:
        continue
    print(report)
```

リポジトリから `python examples/i2c_dcreport.py` を実行すると生データを表示します。
`--decode` を付けるとazarashiで解析します。明示的なCRC補完には`--decode --reconstruct-crc`を使います。空のキューだけでは、
受信機の正常性や測位状態は判定できません。

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

`cxd5610_read_dcreports(ctx, out, capacity, dropped)` は最大 `capacity` 件を消費して、
取得件数または負のerrnoを返します。`out` は `struct cxd5610_dcreport` の配列です。
`capacity=0` の場合は `out=NULL` として、任意の `uint64_t *dropped` に
キューあふれの累計件数だけを取得できます。

`cxd5610_create_with_dcr(&ctx, enabled)` で初期状態を指定できます。
既存の `cxd5610_create(&ctx)` の初期状態は有効です。
`cxd5610_set_qzss_dcr_enabled(ctx, enabled)` は正常応答を待ち、成功時に0、失敗時に負のerrnoを返します。
Cの呼び出し元はshutdownと同時に実行しないでください。

`cxd5610_wait_ready(ctx, timeout_ms)` で有限時間だけ設定確認を待ち、
`cxd5610_get_session_status(ctx, &status)` で要求値・適用値・障害理由を読み出せます。
復旧不能な読み出し・設定・完了待ちは `CXD5610_SESSION_ERROR`（`-ENOTRECOVERABLE`）を返します。
元のerrnoと変更/復元の両失敗理由は `struct cxd5610_session_status` に保持します。
統計だけの `cxd5610_read_dcreports(ctx, NULL, 0, &dropped)` は障害後も使用できます。
