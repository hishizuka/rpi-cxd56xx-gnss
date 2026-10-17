# rpi-cxd56xx-gnss

```sh
pip install rpi-cxd56xx-gnss
```

![Raspberry Pi with a Sony Spresense GNSS Add-on Board](https://raw.githubusercontent.com/hishizuka/rpi-cxd56xx-gnss/main/docs/images/rpi-cxd56xx-gnss.jpg)

English | [日本語](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/README.ja.md)

Sony CXD56xx GNSS receivers on Raspberry Pi: a Linux I2C driver, Python bindings
and a command-line reader. Read position, altitude, speed, heading, satellite
data and UTC time with the `CXD56xx` class or `cxd56xx-gnss` command.
The installed Python API and CLI support **I2C only**.

Requires **Linux, Python 3.13+ and libgpiod 2.0+**. Packages are built from source
on the target system; Raspberry Pi OS Trixie or later is recommended.

## Quick start

Follow the [hardware setup](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/docs/setup.md)
to connect **SDA, SCL and IRQIN through 1.8 V level shifting**, enable I2C at
**400 kHz**, and grant I2C/GPIO access.

```sh
sudo apt install build-essential python3-dev python3-venv libgpiod-dev gpiod
python3 -m venv .venv
. .venv/bin/activate
CXD56XX_GPIO_CHIP=/dev/gpiochip0 python -m pip install --no-cache-dir rpi-cxd56xx-gnss
cxd56xx-gnss
```

Replace `/dev/gpiochip0` with the chip containing your interrupt GPIO, identified
using `gpiodetect` and `gpioinfo`. The default is `/dev/gpiochip4`, line offset
`17`. The chip is selected at build time; changing it requires reinstalling.
Press **Ctrl+C** to stop the reader.

## Tested I2C hardware

| Board | Chip | Bands | Validation |
| --- | --- | --- | --- |
| [Spresense GNSS Add-on Board](https://developer.spresense.sony-semicon.com/products/gnss-add-on-board) | CXD5610GF | L1 + L5 | 3D fix confirmed on Pi Zero 2 W |

CXD5605 I2C compatibility is unverified.

## Standalone UART example

The repository includes a separate pySerial [UART example](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/examples/uart_read.py)
for startup commands, band selection and raw NMEA output. It is **not installed
by pip** and does not use the `CXD56xx` API.

UART connections have been tested with these boards:

| Board | Chip | Bands |
| --- | --- | --- |
| [REYAX RYS8833_Lite](https://reyax.com/product/GPS-GNSS/RYS8833) | CXD5605AGF | L1 |
| [REYAX RYS8839_Lite](https://reyax.com/product/GPS-GNSS/RYS8839) | CXD5610GF | L1 + L5 |

## Additional tools

Run these tools from a repository checkout or unpacked source distribution;
they are not installed as pip commands.

- [Timed I2C observation](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/docs/i2c-window-test.md) records communication, fixes and update intervals separately.
- [RTCM recording](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/docs/rtcm.md) collects and checks I2C observations, with an optional `screen` launcher.
- [Firmware update](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/docs/firmware-update.md) validates an externally obtained v144 package offline and provides an explicit I2C write mode for the Spresense GNSS Add-on Board. The separate RYS8839 UART updater is experimental; firmware compatibility is unverified.

## Documentation

- [Setup](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/docs/setup.md) — wiring diagrams, pin mapping, I2C clock and GPIO settings.
- [Python and C API](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/docs/api.md) — examples, properties and building from a checkout.
- [UART example](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/docs/uart.md) — standalone pySerial usage, startup commands and band selection.
- [Releasing](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/docs/releasing.md) — GitHub, TestPyPI and PyPI publishing.

## License and origin

[Apache License 2.0](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/LICENSE).
Extracted from [Pi Zero Bikecomputer](https://github.com/hishizuka/pizero_bikecomputer)
into a standalone library, which the original application now uses as an
external package. Includes code adapted from Apache NuttX; see
[NOTICE](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/NOTICE) and
[THIRD_PARTY.md](https://github.com/hishizuka/rpi-cxd56xx-gnss/blob/main/THIRD_PARTY.md).
This is an independent project, unaffiliated with Sony or the Apache Software Foundation.
