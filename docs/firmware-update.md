# Firmware update tools

English | [日本語](ja/firmware-update.md)

[README](../README.md) · [I2C setup](setup.md) · [UART](uart.md) · [RTCM](rtcm.md)

These are standalone tools in `examples/`. Run them from the repository root
or an unpacked source distribution. They are not installed as pip commands.
The Raspberry Pi tools are this project's implementations; Sony's
[official updater procedure](https://developer.spresense.sony-semicon.com/ja/downloads/gnss-addon-fw-updater/manual)
uses a Spresense main board.

| Tool | Target | Validation |
| --- | --- | --- |
| `i2c_firmware_update.py` | Spresense GNSS Add-on Board, CXD5610GF | v00.103 → v00.144 over I2C is recorded in the [test results](ja/rtcm.md#i2c-firmware-update) |
| `uart_firmware_update.py` | Experimental RYS8839/CXD5610GF path | Firmware compatibility and a successful hardware update are unverified; not for RYS8833/CXD5605 |

Both tools default to an offline plan. Only `--write` enables firmware writes.
The I2C tool also supports `--query`, which only requests the installed version.

## Package and prerequisites

Obtain the [Sony GNSS Add-on FW Updater v144 package](https://developer.spresense.sony-semicon.com/file/download/gnss-add-on-fw-updater-v144)
under Sony's terms and store it outside the checkout. Firmware is not bundled.
`--package` accepts the original ZIP or its SPK. The tools check exact sizes
and SHA-256 hashes for the package and each embedded image before device access;
other releases or repacked files are rejected. The I2C-only `--image-dir`
alternative accepts the five extracted images listed in the script, with the
same image checks.

Offline planning uses Python's standard library. I2C device access requires
Linux and libgpiod's Python bindings version 2 or later. UART writes use
pySerial and a POSIX serial port supporting exclusive access. On Raspberry Pi
OS Trixie, using the system `python3`:

```sh
sudo apt install gpiod python3-libgpiod python3-serial
```

The Debian packages provide the [GPIO bindings](https://packages.debian.org/trixie/python3-libgpiod)
and [serial module](https://packages.debian.org/trixie/python3-serial).
If using a virtual environment, it must have access to those modules.
Neither updater requires installing the `cxd56xx_gnss` extension.

For I2C, complete the [wiring and permission setup](setup.md), including 1.8 V
level shifting. Use `gpiodetect` and `gpioinfo` to identify the actual IRQ chip
and line. The commands below use `/dev/gpiochip0` and line 17 as examples;
replace them for your board. Stop other readers and RTCM recording first.
Use stable power and do not interrupt a firmware write.

## Offline plan

```sh
python3 examples/i2c_firmware_update.py \
  --package "/path/to/gnss_addon_fw_updater_v144.zip"
```

No I2C or GPIO device is opened. Inspect `summary.json` for `mode=offline_plan`,
`device_opened=false`, the image list and frame counts. This checks the input;
it does not establish compatibility with a different module.

## I2C version query and write

First read the installed version without providing a firmware package:

```sh
python3 examples/i2c_firmware_update.py --query \
  --gpio-chip /dev/gpiochip0 --irq-line 17
```

The I2C defaults are `/dev/i2c-1`, address `0x24`. Override them with
`--i2c-device` and `--address`. A write must match `--expected-version`
(default `v00.103`); a receiver already at `v00.144` is left unchanged.

```sh
python3 examples/i2c_firmware_update.py --write \
  --package "/path/to/gnss_addon_fw_updater_v144.zip" \
  --gpio-chip /dev/gpiochip0 --irq-line 17 --expected-version v00.103
```

The default `--enter-mode force` requests update mode without an erase
command. On the tested v00.103 board it was rejected. The tool stops and does
not switch modes automatically. A separate invocation with
`--write --enter-mode erase` explicitly **erases eMRAM**, then transfers the
RAM updater, SDK, application, library and **Spresense-specific configuration**.
That is the mode recorded as successful on the tested Spresense board. It is
not a generic recovery method for other modules, and there is no automatic
backup or rollback.

Each transfer and image-write acknowledgement is checked. Completion requires
a reset followed by a version query returning `v00.144`. Full eMRAM readback
comparison has not been implemented. After updating, run the ordinary reader
and, if needed, the [RTCM test](rtcm.md) separately.

<a id="experimental-uart-update"></a>

## Experimental UART update

The UART tool reuses the audited Spresense v144 updater/SDK/application/library
images but excludes the Spresense configuration. This restriction does not
prove RYS8839 firmware compatibility. Treat it as an experimental tool.

Start with the offline plan, which requires a fresh log directory outside Git:

```sh
python3 examples/uart_firmware_update.py \
  --package "/path/to/gnss_addon_fw_updater_v144.zip" \
  --log-dir "$HOME/.local/state/rpi-cxd56xx-gnss/uart-plan-example"
```

For an explicitly chosen hardware experiment, the write invocation is:

```sh
python3 examples/uart_firmware_update.py --write \
  --package "/path/to/gnss_addon_fw_updater_v144.zip" \
  --port /dev/serial0 --baudrate 115200 --expected-version v00.129 \
  --wait-for-reset 30 \
  --log-dir "$HOME/.local/state/rpi-cxd56xx-gnss/uart-write-example"
```

Use the actual port, baud rate and installed version; `v00.129` is the tool's
default expected version, not a compatibility guarantee. Follow the board's
UART voltage requirements and disable any serial console as described in the
[UART guide](uart.md). The tool sends `@VER` and `@GSTP`, then polls ForceBoot
for `--wait-for-reset` seconds. It does not operate a reset GPIO; any physical
reset is part of entering ForceBoot, before firmware transfer begins.

The UART tool forbids erase and configuration-write commands. After uploading
and executing the RAM updater, it stops if normal firmware still answers the
version request. Firmware frames are not retried automatically. Final success
still requires an independent `v00.144` version response, even if the binary
boot acknowledgement was lost.

`--resume-bootloader` is only for a receiver already verified to be in its
bootloader after an earlier interrupted attempt. It skips the initial installed
version check and ForceBoot entry; `--expected-version` is not checked in that
mode. Do not use it to bypass an unexpected version or as a blind retry.

## Logs and failures

Both tools write `events.jsonl` and `summary.json`; the UART tool also records
`uart-rx.bin`, which can contain NMEA coordinates. Logs include command-line
arguments and local paths. Keep them outside Git and review them before sharing.
New log directories are private and cannot be reused. The I2C default is under
`$XDG_STATE_HOME/rpi-cxd56xx-gnss/` or `~/.local/state/rpi-cxd56xx-gnss/`.

Exit code `0` means the requested operation completed; `1` means a caught
update/query error or interruption. Invalid arguments use argparse's code `2`;
invalid input files fail before device access. `completed=true` in an offline
plan means validation only. For a write, check `verified_version` as well.
On failure, preserve the logs and inspect the last acknowledged phase before
attempting recovery. Neither tool automatically retries firmware writes,
erases, resets after an error or rolls back partially written images.
