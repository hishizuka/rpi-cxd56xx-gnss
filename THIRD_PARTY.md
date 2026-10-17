# Third-party code and provenance

## Pi Zero Bikecomputer

- Repository: https://github.com/hishizuka/pizero_bikecomputer
- Source checkout: `278fb822c610d086460bb8a7cd751d2ec27fdd53`
- Latest change to the extracted driver files: `a3ef7c24834b83859862055ef8842a2765f58ccb`
- First CXD5610 import: `976c4b28ad6214c3bcd6cdc565e65b422df983a3`
- NuttX attribution added: `e696ae5c3da79b6afd70840b4d65aaaf7adef212`
- License in the source repository: GPLv3, with Apache-2.0 notices for the NuttX portions.

The Sony CXD56xx GNSS driver was extracted into this standalone library.
Pi Zero Bikecomputer now uses it as an external package and no longer bundles
these driver sources. The extracted paths and original SHA-256 hashes are
recorded in [docs/source-manifest.json](docs/source-manifest.json).

The Linux port and Python additions by hishizuka are made available under
Apache-2.0 in this repository. This does not change the license of the original
Pi Zero Bikecomputer project or revoke permissions granted for earlier releases.

| Original path | Destination |
| --- | --- |
| `modules/sensor/gps/cython/cxd5610_rpi.c` | `src/cxd56xx_gnss/cxd5610_rpi.c` |
| `modules/sensor/gps/cython/cxd5610_rpi.h` | `src/cxd56xx_gnss/cxd5610_rpi.h` |
| `modules/sensor/gps/cython/cxd5610_helper.pyx` | `src/cxd56xx_gnss/_cxd5610.pyx` |
| `modules/sensor/gps/cython/cxd5610_helper.pyxbld` | Replaced by `setup.py` and `pyproject.toml` |
| `modules/sensor/gps/cython/build.py` | Adapted into `src/cxd56xx_gnss/cli.py` |

## Apache NuttX

- Repository: https://github.com/apache/nuttx
- License: Apache License, Version 2.0; see [LICENSE](LICENSE).
- Driver: `boards/arm/cxd56xx/drivers/sensors/cxd5610_gnss.c`
- Related public header inspected: `include/nuttx/sensors/cxd5610_gnss.h`
- Local affected files: `src/cxd56xx_gnss/cxd5610_rpi.c` and `cxd5610_rpi.h`.

The original Pi Zero Bikecomputer notices identify NuttX GNSS protocol code as
the source of portions of the port, but do not record the exact upstream commit
or a complete line-level mapping. That historical revision remains unknown.

The upstream source headers and NOTICE were checked at
`87f134cfaa9e1474515deffb1e1224ab16f5b48e`. This was the latest commit touching
the upstream driver before the initial Pi Zero Bikecomputer import, as returned
by the GitHub path history. It is a reproducible comparison baseline, **not a
claim that this was the revision originally used**.

- [Reference driver](https://github.com/apache/nuttx/blob/87f134cfaa9e1474515deffb1e1224ab16f5b48e/boards/arm/cxd56xx/drivers/sensors/cxd5610_gnss.c)
- [Reference header](https://github.com/apache/nuttx/blob/87f134cfaa9e1474515deffb1e1224ab16f5b48e/include/nuttx/sensors/cxd5610_gnss.h)
- [Reference NOTICE](https://github.com/apache/nuttx/blob/87f134cfaa9e1474515deffb1e1224ab16f5b48e/NOTICE)

The applicable ASF attribution is retained in [NOTICE](NOTICE), and the ASF
source license notice is preserved in the affected source files.

The Linux port changes I2C and GPIO access, synchronization, notification data
handling and the public API. The extraction adds standalone build metadata,
Python package imports, CLI packaging, documentation and overridable build
defaults. The GNSS protocol still follows the original port.

## Sony SIoT-Majipro update protocol

- Repository: https://github.com/sony/SIoT-Majipro
- Reference revision: `36d30533c9e419389b4367d2448bb6ce8aa2dfaa`.
- [Reference updater](https://github.com/sony/SIoT-Majipro/blob/36d30533c9e419389b4367d2448bb6ce8aa2dfaa/applications/fwupdate/cxd5610/src/update_main.c).
- Source header: Copyright 2024 Sony Group Corporation, SPDX `Apache-2.0`.
- Local files: `examples/i2c_firmware_update.py` and the shared packet/image
  helpers used by `examples/uart_firmware_update.py`.

The update command sequence and framing were adapted to Python, Linux I2C/GPIO
and UART. The local tools add package/image hash validation, explicit write
modes, version checks and private logs. No Sony firmware image is distributed.
The Spresense v144 package must be obtained separately under Sony's terms;
the source-code license does not grant rights to redistribute its firmware.

## Dependencies

libgpiod is an external system library; Cython and setuptools are build
dependencies. The UART example uses pySerial. Their sources and binaries are
not bundled. No Sony SDK, firmware or datasheet is included in this repository.
