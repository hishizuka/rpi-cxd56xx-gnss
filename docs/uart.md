# UART startup and band selection

English | [日本語](ja/uart.md)

[README](../README.md) · [I2C setup](setup.md) · [Python and C API](api.md)

[examples/uart_read.py](../examples/uart_read.py) sends startup commands to
REYAX RYS8833_Lite / RYS8839_Lite modules and prints their NMEA output.
It uses pySerial directly and does not require the I2C native extension or libgpiod.

This standalone example is not installed by pip and does not provide the
`CXD56xx` API. The package API and CLI support **I2C only**.

## Connect and run

On Raspberry Pi, use `sudo raspi-config` to disable the serial login shell and
enable the serial hardware. The REYAX boards use 1.8 V UART signals: connect
host TX to board RX and host RX to board TX through level translation, with
common GND. Check power and pin assignments in the
[RYS8833_Lite](https://reyax.com/upload/products_download/download_file/RYS8833_Lite.pdf)
and [RYS8839_Lite](https://reyax.com/upload/products_download/download_file/RYS8839_Lite_EN.pdf)
product documents.

Download the example or run it from a repository checkout:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install pyserial

# RYS8833_Lite: L1
python examples/uart_read.py /dev/ttyS0 --gns 47

# RYS8839_Lite: L1 + L5
python examples/uart_read.py /dev/ttyS0 --gns 16367
```

Use your serial device path. Defaults are **115200 bps, 8N1**; use
`--baudrate 9600`, for example, if the receiver is configured differently.
After initialization, the script prints NMEA until **Ctrl+C** closes the port.
`--init-only` exits after starting positioning. The script does not send `@GSTP`
on exit, so the receiver keeps positioning.

## Startup commands

UART receivers also need positioning stopped, satellite selection configured,
and positioning restarted at startup. For RYS8839_Lite, the sequence is:

```text
@GSTP
@GGNS
@GNS 16367
@GGNS
@GSR
```

Terminate each command with **CRLF (`\r\n`)** and wait for its `[command] Done`
response before sending the next one. The example performs this sequence
automatically, waiting up to five seconds per command. An `Err` response or a
timeout stops the sequence.

| Command | Purpose |
| --- | --- |
| `@GSTP` | Stop positioning and enter Idle for configuration |
| `@GGNS` | Read the current satellite settings, before and after the change |
| `@GNS <mask>` | Select constellations and signals with a bitmask |
| `@GSR` | Start positioning with a hot start |

References: [RYS8833_Lite Quick Start Guide](https://reyax.com/upload/products_download/download_file/RYS8833_Lite.pdf#page=6)
and [RYS8839 Software Guide](https://reyax.com/upload/products_download/download_file/RYS8839_Software_Guide.pdf#page=9).
These ASCII commands are for UART; the I2C driver uses a binary protocol.

## Band and constellation selection (`--gns`)

Set each desired signal's bit to `1`. Bit `n` has value `1 << n`; combine values
with bitwise OR (or add distinct bit values). The example accepts decimal and
`0x`-prefixed hexadecimal masks.

The following assignments apply to UART `@GNS`, as documented in the
[RYS8839 Software Guide §3.3.37](https://reyax.com/upload/products_download/download_file/RYS8839_Software_Guide.pdf#page=40)
and [RYS8830/RYS8833 Software Guide §12](https://reyax.com/upload/products_download/download_file/RYS8830_RYS8833_Software_Guide.pdf).

| Bit | Decimal (hex) | Signal |
| --- | --- | --- |
| 0 | `1` (`0x0001`) | GPS L1-C/A |
| 1 | `2` (`0x0002`) | GLONASS L1OF |
| 2 | `4` (`0x0004`) | SBAS (L1) |
| 3 | `8` (`0x0008`) | QZSS L1-C/A |
| 4 | `16` (`0x0010`) | Reserved; keep clear |
| 5 | `32` (`0x0020`) | QZSS L1-S |
| 6 | `64` (`0x0040`) | BeiDou B1I |
| 7 | `128` (`0x0080`) | Galileo E1B/C |
| 8 | `256` (`0x0100`) | GPS L5 |
| 9 | `512` (`0x0200`) | QZSS L5 |
| 10 | `1024` (`0x0400`) | BeiDou B1C (L1 band) |
| 11 | `2048` (`0x0800`) | BeiDou B2a (L5 band) |
| 12 | `4096` (`0x1000`) | Galileo E5a (L5 band) |
| 13 | `8192` (`0x2000`) | NavIC (L5) |

CXD5605 (RYS8833_Lite) uses the L1 signals in bits 0–7, excluding bit 4.
Bits 8–13 apply to CXD5610 (RYS8839_Lite). **BeiDou B1C, bit 10, is an L1
signal**; the L5 bits are 8, 9, 11, 12 and 13.

**RYS8833 cannot select GLONASS (bit 1) and BeiDou (bit 6) together.** BeiDou
availability also depends on firmware. Do not use `239` (`0x00ef`) on RYS8833,
because it enables both constellations.

| Module | Mask | Selection |
| --- | --- | --- |
| RYS8833 / RYS8839 | `1` (`0x0001`) | GPS L1-C/A |
| RYS8833 / RYS8839 | `47` (`0x002f`) | GPS L1, GLONASS, SBAS, QZSS L1-C/A and L1-S |
| RYS8833 / RYS8839 | `109` (`0x006d`) | GPS L1, BeiDou B1I, SBAS, QZSS L1-C/A and L1-S |
| RYS8833 / RYS8839 | `173` (`0x00ad`) | GPS L1, Galileo E1, SBAS, QZSS L1-C/A and L1-S |
| RYS8839 | `813` (`0x032d`) | GPS L1/L5, QZSS L1-C/A/L1-S/L5, SBAS |
| RYS8839 | `1263` (`0x04ef`) | All listed L1 signals, including BeiDou B1C |
| RYS8839 | `16367` (`0x3fef`) | All listed signals: bits 0–13 except bit 4 |

For example, `47 = 1 + 2 + 4 + 8 + 32`. To select GPS/QZSS L1/L5 and SBAS:

```python
# GPS L1 + SBAS + QZSS L1-C/A + QZSS L1-S + GPS L5 + QZSS L5
gns = (1 << 0) | (1 << 2) | (1 << 3) | (1 << 5) | (1 << 8) | (1 << 9)
print(gns, hex(gns))  # 813 0x32d
```

```sh
python examples/uart_read.py /dev/ttyS0 --gns 0x032d
```

Change settings while positioning is stopped, then verify with `@GGNS`.
The example does this automatically. Reception also depends on the antenna's
supported bands and the reception environment.

## I2C satellite settings

The `CXD56xx` class and [I2C example](../examples/i2c_read.py) currently use fixed
settings in the C driver. `--gns` belongs to the UART example. To change I2C
satellite settings, edit `sat_setting` in `configure_session()` in
[cxd5610_rpi.c](../src/cxd56xx_gnss/cxd5610_rpi.c) according to the receiver's
binary protocol specification and rebuild. Do not substitute a UART mask
directly for the binary protocol settings.
