# Python and C API

English | [日本語](ja/api.md)

[README](../README.md) · [Setup](setup.md) · [UART](uart.md)

## Python

After [installing from PyPI](setup.md#4-select-the-gpio-chip-and-install-from-pypi),
import `CXD56xx`. Its constructor starts a background worker that stops
positioning, configures satellites and binary notifications, then starts
positioning again. The Python caller does not need to send startup commands.

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

A runnable version is in [examples/i2c_read.py](../examples/i2c_read.py).
Download it or run `python examples/i2c_read.py` from a checkout with your virtual
environment active.

`close()` waits for the GNSS stop acknowledgement and releases I2C/IRQ resources.
It is idempotent and raises `OSError` on shutdown failure. Call it in `finally`
to check shutdown errors; `peek()` after closing raises `ValueError`.

`peek()` is non-blocking. It returns `0` for a successful snapshot,
`-errno.EAGAIN` while waiting for the first notification, or another negative
errno on ordinary errors. Terminal failures raise `CXD56xxSessionError`.
Read properties only after success. Without a new notification,
it returns the previous snapshot; compare `timestamp` to detect updates.

Successful reception does not necessarily mean a position fix.
`mode=3` / `status=1` indicates a 3D fix. `mode=2` indicates a 2D fix;
`status=2` is a valid differential GPS fix.

| Property | Meaning |
| --- | --- |
| `lat`, `lon` | Latitude and longitude in degrees |
| `alt` | Altitude in metres, as received; no additional geoid correction |
| `speed` | Speed in m/s |
| `track` | Heading in degrees |
| `mode`, `status` | Fix state |
| `pdop`, `hdop`, `vdop` | Dilution of precision |
| `used_sats`, `total_sats` | Tracking flags / notification entries; separate signals from the same satellite can be counted more than once |
| `timestamp` | UTC Unix time in seconds |

Missing values are `None`. Failure to open a device during initialization raises
`OSError`. Satellite selection other than QZSS L1S is fixed in the I2C driver; see the
[UART guide](uart.md#i2c-satellite-settings) for the distinction between protocols.
Standalone [RTCM recording](rtcm.md) and [firmware update tools](firmware-update.md)
have separate dependencies and commands; they are not part of this API.

## Configuration confirmation and session failures

Construction starts the worker asynchronously. `wait_ready(timeout=15.0)` waits
for successful satellite, notification and START acknowledgements, then returns
`True`; a position fix is unnecessary. Each command has a three-second deadline.
Startup and automatic recovery each make one configuration attempt. Only STOP
retains quiet-start compatibility for `ETIMEDOUT`/`EAGAIN`; missing configuration
or START acknowledgements fail the session.

Call `await asyncio.to_thread(gps.wait_ready, 15.0)` from async code. A caller's
wait expiring first raises `TimeoutError` while the worker continues. Waiting
uses a monotonic clock, independently of UTC corrections.
One deadline starts at Python method entry and includes acquisition of the
internal lifecycle lock. Only the remaining budget is passed to C; time spent
waiting for the lock is not added again. `timeout=0` never waits for the lock:
it returns `True` only if the lock is immediately available and configuration
is confirmed, otherwise raising `TimeoutError`. The remaining C budget is
rounded down to milliseconds; less than 1 ms allows only a state check.
A timeout does not interrupt another setting change or the receiver worker.

`session_status` returns an immutable exported `SessionStatus`:

| Field | Meaning |
| --- | --- |
| `state` | `starting`, `ready`, `configuring`, `recovering` or `failed`; ready confirms configuration, not a fix |
| `qzss_dcr_requested` | Setting to maintain; a pending change reports its new request, successful restoration reports the previous setting |
| `qzss_dcr_applied` | Confirmed boolean, or `None` during startup, recovery, changes and after terminal failure |
| `error_errno`, `error_stage` | Positive terminal errno and stage (`startup`, `recovery`, `restore`, `transport`, `worker`); `None` without failure |
| `change_errno`, `restore_errno` | Positive errno for the most recent change/restoration failures, or `None` |

A failed change with successful restoration raises ordinary `OSError`.
Terminal failures raise exported `CXD56xxSessionError(OSError)` with `errno`,
`stage`, `change_errno` and `restore_errno` attributes. Catch this subclass before
ordinary `OSError`. Subsequent `peek()`, `read_dcreports()`, `wait_ready()` and
setting changes report the same terminal failure; cached positions and queued
reports are invalidated. Status and overflow statistics remain readable.
Close and recreate a failed receiver. Close releases resources even if it
raises `OSError`; all methods/status access after close raise `ValueError`.

Python serializes setting changes, readiness waits and close. Cancelling an
`asyncio.to_thread()` await does not stop the C call; close waits for that call
before releasing resources. Manage task completion and application state too,
and run blocking methods outside the Qt event loop.

## QZSS disaster and crisis reports (I2C)

`CXD56xx(qzss_dcr_enabled=True)` enables QZSS L1S reception and binary DC report
notifications (`0x8b`) at startup. Pass `False` to start with them disabled.
`set_qzss_dcr_enabled(enabled)` changes receiver settings at runtime without
stopping positioning. It waits for both command acknowledgements and returns
`True` on success. Use `asyncio.to_thread` from an async application:

```python
import asyncio

gps = CXD56xx(qzss_dcr_enabled=False)
await asyncio.to_thread(gps.wait_ready)
await asyncio.to_thread(gps.set_qzss_dcr_enabled, True)
await asyncio.to_thread(gps.set_qzss_dcr_enabled, False)
```

The last successful setting is reapplied during session recovery. QZSS L1-C/A
and L5 positioning signals are retained. Successful changes clear pending DC
reports; reports received while disabled or changing settings are ignored.
Failures raise `OSError` and attempt to restore the previous configuration.
If restoration also fails, `CXD56xxSessionError` is raised; close and recreate the instance.
Startup remains asynchronous: the constructor returning does not confirm
receiver configuration has finished.

`read_dcreports()` drains pending
messages without waiting or calling `peek()`. A position fix is not required.
It returns a `list[DCReport]`, or `[]` when no messages are pending.

`DCReport` is an immutable named tuple exported by `cxd56xx_gnss`:

| Field | Meaning |
| --- | --- |
| `satellite_id` | QZQSM satellite ID, e.g. `55` for PRN 183; Sony's notification ID is used directly |
| `data` | Independent `bytes` copy of the 32-byte L1S message, in received byte order; 250 meaningful bits followed by padding |
| `received_at` | Host UTC Unix receive time in seconds, including fractional seconds; independent of the GNSS timestamp |

The queue holds 64 messages in arrival order. Reading consumes them; repeated
notifications are retained for the application to deduplicate. On overflow,
the oldest pending message is discarded. `dcreports_dropped` is the cumulative
overflow count for this `CXD56xx` instance; querying it does not consume messages
and draining does not reset it. Messages discarded by a setting change do not
increase this overflow count. Reading, statistics and setting changes raise
`ValueError` after `close()`.

The receive path copies all 32 bytes without changing padding, converting to text,
decoding content, or checking the L1S CRC. The I2C packet checksum is checked by
the transport. The decoder should validate the L1S CRC and content.
Both MT43 and MT44 payloads are passed through if the receiver outputs them.
CXD5610 output may be rejected by azarashi due to an L1S CRC mismatch. The
explicit adapter below enables parsing without verifying the received CRC.

### `DCReport.to_azarashi_packet(*, reconstruct_crc=False) -> bytes`

Returns satellite ID + 32 bytes for azarashi's `net` format without device access
or an azarashi dependency. The default uses the received bytes unchanged.
With `True`, CRC24Q of the first 226 bits replaces only the CRC field in a copy.
The original `message.data`, reception time, protected bits and six padding bits
remain unchanged. Reconstruction enables parsing; it does not validate the
received CRC. Retain `message.data` when recording the original reception.

```python
packet = message.to_azarashi_packet(reconstruct_crc=True)
report = azarashi.decode(
    packet, "net",
    timestamp=datetime.fromtimestamp(message.received_at, timezone.utc),
)
```

The library handles this CXD5610 adaptation; applications pass the returned
packet to their existing azarashi decoder. A non-bool option raises `TypeError`;
invalid IDs or frame data in manually constructed reports raise `ValueError`.

The optional [azarashi API](https://github.com/nbtk/azarashi/blob/main/docs/api.md)
accepts satellite ID + data as its 33-byte `net` format. No UDP connection or
QZQSM conversion is needed. Install `azarashi>=0.17.1` separately to use this example:
The format is passed positionally because released 0.17.1 calls this argument
`msg_type`, while the upstream API documentation calls it `msg_format`.

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

Run `python examples/i2c_dcreport.py` from a checkout to inspect raw reports, or
add `--decode` to decode them. Explicit CRC reconstruction uses
`--decode --reconstruct-crc`. These are independent of position snapshots;
an empty queue does not indicate whether the receiver is healthy or has a fix.

## Building from a checkout

A checkout is useful for changing the driver or using the C executable.
For normal Python use, install from PyPI as shown in the [README](../README.md).

```sh
sudo apt install git build-essential python3-dev python3-venv libgpiod-dev gpiod
git clone https://github.com/hishizuka/rpi-cxd56xx-gnss.git
cd rpi-cxd56xx-gnss
python3 -m venv .venv
. .venv/bin/activate
CXD56XX_GPIO_CHIP=/dev/gpiochip0 python -m pip install .
```

Use the GPIO chip identified in the [setup guide](setup.md). After changing
sources or build settings, reinstall with `--force-reinstall --no-cache-dir`.
The Cython extension is built during installation.

## C executable and library

From the checkout:

```sh
make CXD56XX_GPIO_CHIP=/dev/gpiochip0
./build/cxd5610
```

Plain `make` uses the default chip, or `CXD56XX_GPIO_CHIP` if exported in the
environment. It rebuilds on every invocation so changed settings take effect.

To embed the driver in a C application, compile `cxd5610_rpi.c` with
`CXD5610_NO_MAIN=1` and link `gpiod`, `pthread` and `m`. The API and overridable
defaults are declared in [cxd5610_rpi.h](../src/cxd56xx_gnss/cxd5610_rpi.h).

`cxd5610_read_dcreports(ctx, out, capacity, dropped)` drains up to `capacity`
messages, returning their count or a negative errno. `out` points to an array of
`struct cxd5610_dcreport`. With `capacity=0`, `out` may be `NULL` to query only
the optional cumulative `uint64_t *dropped` counter.

`cxd5610_create_with_dcr(&ctx, enabled)` selects the initial setting;
`cxd5610_create(&ctx)` defaults to enabled.
`cxd5610_set_qzss_dcr_enabled(ctx, enabled)` waits for acknowledgements and
returns zero on success or negative errno on failure. C callers must serialize
it with shutdown.

`cxd5610_wait_ready(ctx, timeout_ms)` waits a finite time for confirmation;
`cxd5610_get_session_status(ctx, &status)` returns requested/confirmed settings
and structured diagnostics. Terminal read/change/wait failures return
`CXD5610_SESSION_ERROR` (`-ENOTRECOVERABLE`); the status preserves the original
errno and separate change/restoration errors. A statistics-only
`cxd5610_read_dcreports(ctx, NULL, 0, &dropped)` still works after failure.
