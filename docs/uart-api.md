# POSIX UART Python API

English | [日本語](ja/uart-api.md)

[README](../README.md) · [UART wiring and satellite masks](uart.md)

`CXD56xxUART` reads NMEA, RTCM3 and Sony binary frames from one POSIX serial
port. It is separate from the existing native I2C `CXD56xx` class. The CLI
`cxd56xx-gnss` still uses I2C. Python 3.13+ and `pynmeagps` are required.

## Install from this checkout

```sh
CXD56XX_BUILD_I2C=0 python -m pip install '.[uart]'
```

This builds a UART-only wheel without compiling the I2C extension or linking
libgpiod. Normal installation still builds the I2C extension; omit the environment
variable and install `.[uart]` on Linux to enable both APIs. UART-only installation
does not provide `CXD56xx` or the I2C CLI.

## Open, start and read

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

The constructor opens the UART and starts receiving. By default it sends no
receiver command; an explicit `qzss_dcr_enabled` argument also performs the
synchronous DCR configuration described below. Set the actual baud rate and
hardware flow control explicitly.
Opening applies host-side 8N1 and clears HUPCL without flushing queued input;
it does not set the receiver's baud rate, toggle GPIO, reset or update firmware.
A file lock excludes other library instances, but cannot exclude an Android HAL
or another process that ignores advisory locks. Stop all other UART owners first.
Ownership switching is the application's responsibility.

`start()` sends `@WUP` (up to ten attempts of 2.5 seconds), `@GSTP`, then `@GSR`.
Stopping before a hot start avoids the busy response from an already-positioning
receiver. `stop()` wakes and sends `@GSTP`. `get_satellite_mask()` wakes and reads
`@GGNS`. `set_l1s_enabled(bool)` wakes, enters Idle, reads the mask, changes only
bit 5, verifies it by readback and starts positioning. **L1S tracking is not a
DCR output switch.** Other satellite settings are preserved.

`close()` waits for in-flight commands, joins the reader and releases the port.
It sends no receiver command. The receiver can keep positioning or emitting RTCM
after close. Call `stop()` or `set_rtcm_mask(0)` explicitly if appropriate.
These methods block; use `asyncio.to_thread` in an asynchronous application.

## Position fields

`peek()` returns `0` for a new RMC epoch, or `-errno.EAGAIN` when there is no new
epoch. Afterwards, `position` is an immutable `UARTPosition`; individual properties
with the same names are also available. Missing values are `None`, never estimates.

| Field | Source / unit |
| --- | --- |
| `lat`, `lon` | Valid RMC, degrees |
| `alt` | Same-epoch GGA altitude, metres **above mean sea level** |
| `geoid_separation` | Same-epoch GGA geoid separation, metres; separate from `alt` |
| `speed` | Valid RMC knots × 1852 / 3600, metres per second |
| `track` | Valid RMC course over ground, degrees true |
| `timestamp` | Valid RMC date/time converted to Unix UTC seconds |
| `mode` | Maximum reported GSA navigation mode in this epoch: 1/2/3; invalid RMC gives 1, absent GSA with valid RMC gives `None` |
| `status` | GGA quality code, not the I2C binary receiver-status enum |
| `pdop`, `vdop` | Last same-epoch GSA values, dimensionless |
| `hdop` | Same-epoch GGA value, dimensionless |
| `used_sats` | Same-epoch GGA number of satellites used |
| `total_sats` | Union of satellite IDs from complete GSV cycles, deduplicated across signals within each constellation |

GGA starts an epoch and RMC finishes it. Time-mismatched GGA and its GSA/GSV
are discarded. Invalid RMC clears coordinates, altitude, motion and timestamp.
GSA and GSV have no timestamps, so this join relies on that output ordering.
An incomplete GSV cycle or mixed combined (`GN`) and per-constellation GSV
returns `None` for `total_sats`. No `epx`, `epy`, `epv` or inferred horizontal
accuracy is generated. The I2C binary altitude's reference datum is not established
by this UART implementation; do not assume the two altitude fields are interchangeable.

## Raw RTCM for carrier-phase applications

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

`set_rtcm_mask(mask)` wakes, enters Idle, sends `@RTCM <hex mask>` and hot-starts.
`0x2` selects GPS MSM7; `0x07102202` selects GPS/Galileo/QZSS MSM7 and their
broadcast ephemerides on v00.201 UART firmware. Output support depends
on receiver firmware. Settings are volatile; no save/configuration-file command
is sent. The previous RTCM mask cannot be queried through this API.

`read_rtcm()` drains complete, **CRC24Q-verified** `RTCMFrame` objects with
`message_type`, original `data` (header, payload and CRC), and host Unix
`received_at`. Position fix is not required to receive frames. Raw frames can be
parsed with optional `pyrtcm`; it is not a library dependency.

A valid frame CRC does not imply valid carrier-phase measurements. MSM7 has
separate missing-value, lock-time and half-cycle fields. The library preserves
them without converting them into an ADR validity claim or position accuracy.

## DCR reception and configuration

The DCR interface matches I2C: `set_qzss_dcr_enabled(bool)`, `read_dcreports()`
and `dcreports_dropped`. An explicit constructor argument
`qzss_dcr_enabled=True/False` applies the same configuration at startup. The
UART default is `None`, retaining passive adoption without receiver commands.

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

Enabling performs WUP → GSTP → GGNS → set GNS bit 5 → verify GGNS →
`BSSL 0x0E0F7FEF` → GSR. Other satellite bits are preserved. Firmware, CFG and
saved settings are not written. This verbose sentence mask supports QZQSM
on v00.201 ASCII UART and also enables diagnostic traffic. Support on
other firmware and the minimum required mask remain unconfirmed. The example
uses 4,000,000 bps with RTS/CTS; diagnostic traffic can exceed a 115200-bps
link. The example assumes that the receiver already uses the shown
UART configuration; this API does not change the receiver baud rate. Acknowledged
L1S settings do not establish satellite acquisition or message reception.

Disabling clears GNS bit 5 and selects the documented BSSL default `0xEF` before
restarting. There is no prior NMEA-mask query, so this does not restore an unknown
original mask. Some firmware may retain diagnostic output; callers manage full
receiver restoration. `close()` sends no commands. A subsequent direct
`set_l1s_enabled()` invalidates the managed DCR setting; configure DCR again
before reading managed reports.

Reports use the same `DCReport(satellite_id, data, received_at)` as I2C. The
63-digit QZQSM hex payload retains its transmitted bits and gains trailing
padding to 32 bytes. `received_at` is host Unix receive time. The DCR body CRC
is neither checked nor used to discard reports, and is not reconstructed by the
reader. The optional shared conversion shown above reconstructs CRC in a copy
for parsing, not validation. Reports can be drained independently of position
fixes or `peek()`.

Reports received during configuration or explicit disable are not queued, and
unread reports from before a change are discarded. The session status exposes
`qzss_dcr_requested` and `qzss_dcr_applied`; `None` means unconfigured or unknown.
Applied `True` means configuration ACKs were received, not that L1S has locked.
A rejection, missing ACK or readback mismatch during this multi-step change
fails the session with `CXD56xxSessionError`. It cannot safely roll back to an
unknown original BSSL mask.

`read_frames()` independently drains `UARTFrame(protocol, message_type, data,
received_at)`. `protocol` is `nmea`, `rtcm`, `binary` or `text`. NMEA XOR, RTCM CRC24Q
and Sony binary header/payload sum checks are verified; plain command-response
text has no wire checksum. Sony binary bodies remain raw, including `0x8B`;
this API does not configure binary notifications or decode their DCR layout.

## Failure and queue behavior

`UARTCommandError` retains a definite receiver rejection in `command` and
`response`. Ordinary single-command rejection leaves the UART open, but may
have stopped positioning; recover explicitly with an appropriate operation such as `start()`.
A missing acknowledgement, read/write failure or ambiguous mask readback makes
the session terminal and raises `CXD56xxSessionError` on subsequent operations.
Close and recreate it after resolving the cause. Only the idempotent wake command
is retried automatically. Reopening does not reset receiver settings.

`session_status` reports the transport state (`ready`, `configuring`, `failed`,
`closed`), last acknowledged RTCM mask, last read-back satellite mask and terminal error. `ready`
means the transport is open, not that positioning or a fix has been confirmed.
Masks initially are `None`; they are not inferred from defaults.

The three receive queues are independent, bounded to `queue_size` objects each
(default 256) and discard the oldest object on overflow. `dropped` reports counts
for `frames`, `rtcm` and `dcreports`; `parse_errors` reports checksum/decoding errors.
`dcreports_dropped` also exposes the report-overflow count with the I2C name.
Drain queues regularly when recording data. Checksummed binary payloads never
satisfy a text command acknowledgement, even if they contain matching text.
