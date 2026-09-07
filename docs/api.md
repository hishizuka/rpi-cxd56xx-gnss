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
    del gps
```

A runnable version is in [examples/i2c_read.py](../examples/i2c_read.py).
Download it or run `python examples/i2c_read.py` from a checkout with your virtual
environment active.

`peek()` is non-blocking. It returns `0` for a successful snapshot,
`-errno.EAGAIN` while waiting for the first notification, or another negative
errno on error. Read properties only after success. Without a new notification,
it returns the previous snapshot; compare `timestamp` to detect updates.

Successful reception does not necessarily mean a position fix.
`mode=3` / `status=1` indicates a 3D fix.

| Property | Meaning |
| --- | --- |
| `lat`, `lon` | Latitude and longitude in degrees |
| `alt` | Altitude in metres, as received; no additional geoid correction |
| `speed` | Speed in m/s |
| `track` | Heading in degrees |
| `mode`, `status` | Fix state |
| `pdop`, `hdop`, `vdop` | Dilution of precision |
| `used_sats`, `total_sats` | Satellite counts; `used_sats` counts tracking flags |
| `timestamp` | UTC Unix time in seconds |

Missing values are `None`. Failure to open a device during initialization raises
`OSError`. Satellite selection is currently fixed in the I2C driver; see the
[UART guide](uart.md#i2c-satellite-settings) for the distinction between protocols.

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
