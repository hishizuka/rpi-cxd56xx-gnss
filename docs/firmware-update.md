# Firmware update tools

English | [日本語](ja/firmware-update.md)

[README](../README.md) · [I2C setup](setup.md) · [UART](uart.md) · [RTCM](rtcm.md)

These are standalone tools in `examples/`. Run them from the repository root
or an unpacked source distribution. They are not installed as pip commands.
Keep the `examples/` directory together when copying these tools: all three
updaters import `_firmware_common.py` from the same directory.
The Raspberry Pi tools are this project's implementations; Sony's
[official updater procedure](https://developer.spresense.sony-semicon.com/ja/downloads/gnss-addon-fw-updater/manual)
uses a Spresense main board.

| Tool | Target | Behavior and scope |
| --- | --- | --- |
| `i2c_firmware_update.py` | Spresense GNSS Add-on Board, CXD5610GF | Five-image v144 update and an experimental v201-code/original-Sony-CFG reflash profile |
| `uart_firmware_update.py` | Experimental RYS8839/CXD5610GF path | v144 code images without CFG or erase commands; firmware compatibility is unverified. Not for RYS8833/CXD5605 |
| `i2c_firmware_update_preserve.py` | I2C code-only update and inspection | Accepts audited Sony v144 or externally supplied v201 code images; blocks CFG, erase and settings writes at the transport boundary |

All tools default to an offline plan. Only `--write` enables firmware writes.
The I2C tools also support `--query`; the preserving tool reads operational
settings in addition to the installed version.

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
None of these updaters requires installing the `cxd56xx_gnss` extension.

## I2C code-only updates retaining the board configuration

`examples/i2c_firmware_update_preserve.py` is separate from the five-image
updater. Select `--profile v144` or `--profile v201` to use fixed image
names, sizes, SHA-256 hashes and a target version. Firmware is not bundled;
externally supplied v201 requires hardware validation on each target board.

The image directory must contain only that release's RAM updater, SDK, APP
and LIB. The updater is named `cxd5610_updater.efpk` for v201 and
`updater.efpk` for v144. CFG files, mixed releases and unexpected files are
rejected before opening devices. There is no arbitrary-image bypass.

```sh
python3 examples/i2c_firmware_update_preserve.py \
  --profile v201 --image-dir "/path/to/audited-v201-code-only"
```

This defaults to an offline plan with 249 injection frames and only the
SDK/APP/LIB program operands `000000`, `010000` and `020000`.
Use `--query --gpio-chip /dev/gpiochip0 --irq-line 17` instead of the image
directory to read the installed version and documented operational settings.
Use the verified GPIO chip for the target board.

An experimental write after verifying the target hardware, current version
and input images is:

```sh
python3 examples/i2c_firmware_update_preserve.py --write \
  --profile v201 --image-dir "/path/to/audited-v201-code-only" \
  --expected-version v00.144 --gpio-chip /dev/gpiochip0 --irq-line 17
```

`--expected-version` identifies the installed version before the update.
Rejected ForceBoot stops the operation; there is no erase fallback. All RAM
injection and execution acknowledgements are required. If normal firmware
still answers VER after RAM execution, no SDK/APP/LIB write is sent.
Success requires the target version after all three program writes.

For a coordinated physical-reset experiment, add `--wait-for-reset 120`.
After `waiting_for_physical_reset` is logged, reset only the GNSS while
keeping the Pi running. No reset GPIO is operated by the tool. Only complete
normal-firmware rejections (`ea`/-22 or -2), startup indications `01`/`02`
and command-write NACK/EIO errors allow
repeating ForceBoot during this entry window. IRQ timeouts, read errors,
short writes and invalid packets stop the operation. Firmware images are
never retried. A candidate entry response still requires RAM transfer,
execution and the normal-firmware version probe before code programming.

Alternatively, `--reboot-before-force` explicitly sends the non-erasing
`0x00 / 01` reboot and waits for its acknowledgement before ForceBoot. These
entry strategies are mutually exclusive. Spresense v00.144 may reject
ForceBoot with `ea` (-22); this code-only path cannot continue when entry is
rejected. The original-Sony-CFG reflash profile below is a separate,
explicitly erasing path.

The wire log can establish that no CFG or erase command was sent. There is no
full CFG readback, byte-for-byte preservation proof, or cross-version CFG
compatibility guarantee. Compare the settings queries and positioning/RTCM
before and after updating. Treat dynamic TCXO values separately.

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
command. Spresense v00.103 may reject this request. A rejection stops the
tool; it does not switch modes automatically. A separate invocation with
`--write --enter-mode erase` explicitly **erases eMRAM**, then transfers the
RAM updater, SDK, application, library and **Spresense-specific configuration**.
This mode is specific to the Spresense board. There is no automatic backup
or rollback.

Each transfer and image-write acknowledgement is checked. Completion requires
a reset followed by a version query returning `v00.144`. Full eMRAM readback
comparison has not been implemented. After updating, run the ordinary reader
and, if needed, the [RTCM test](rtcm.md) separately.

`v144 --write --enter-mode erase` also uses Sony's 100 ms body wait and
IRQ-high check during update mode, allowing ROM bootloaders to keep IRQ high
between the header and body. Ordinary positioning reads retain their edge wait.
If erase entry times out before any image transfer, a matching failed log can
be supplied with `--resume-after-erase /path/to/failed-update-log`. After a live
bootloader check, transfer starts with the RAM updater, without another erase,
STOP command or automatic retransmission of a partially transferred image.

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

All tools write `events.jsonl` and `summary.json`; the UART tool also records
`uart-rx.bin`, which can contain NMEA coordinates. Logs include command-line
arguments and local paths. Keep them outside Git and review them before sharing.
New log directories are private and cannot be reused. The I2C default is under
`$XDG_STATE_HOME/rpi-cxd56xx-gnss/` or `~/.local/state/rpi-cxd56xx-gnss/`.

Exit code `0` means the requested operation completed; `1` means a caught
update/query error or interruption. Invalid arguments use argparse's code `2`;
invalid input files fail before device access. `completed=true` in an offline
plan means validation only. For a write, check `verified_version` as well.
On failure, preserve the logs and inspect the last acknowledged phase before
attempting recovery. None of the tools automatically retries firmware writes,
erases, resets after an error or rolls back partially written images.

## v201 with the original Sony CFG restored

`i2c_firmware_update.py --profile v201` is an experimental
Spresense-only profile. It combines audited v201 RAM updater/SDK/APP/LIB
with `spresense_i2c.cfg` from the official Sony v144 package (3,008 bytes,
SHA-256 `f903fe844e52d9d014eb86a4d72150d6b53ca9cae8857f6789562838b7eb7b0c`).
The CFG is specific to the Spresense board. The file content is retained,
but the procedure **erases eMRAM and rewrites code and the original Sony CFG**.
Full installed-CFG readback is unavailable, and the v201 combination requires hardware
validation. This is separate from the tool that forbids all CFG writes.

Create an offline plan first:

```sh
python3 examples/i2c_firmware_update.py \
  --profile v201 --enter-mode erase \
  --package "/path/to/gnss_addon_fw_updater_v144.zip" \
  --code-dir "/path/to/audited-v201-code-only"
```

Check the 250 injection frames and SDK/APP/LIB/CFG program operands
`000000`, `010000`, `020000`, `030100`. No device is opened. The complete Sony
package and all four v201 code files are checked against fixed hashes. CFG
files in the code directory and substitutions for the Sony CFG are rejected.

Only when choosing erase and restoration of the original Sony CFG, append
`--write --expected-version v00.144 --gpio-chip /dev/gpiochip0 --irq-line 17`
to that command, using the GPIO chip actually verified on the Pi. A v201 write
requires explicit `--enter-mode erase`. Erase follows input validation and
the installed-version check. Success requires every acknowledgement and a
final independent version query returning `v00.201`. There is no automatic
retry, erase/reset after an error or rollback.

An erase-response timeout does not establish that erase failed. Keep power on
and identify the pending response and packet phase before recovery. In the
bootloader, IRQ can stay high between the header and body. During update mode
only, this profile follows Sony's 100 ms body wait and permits reading the
declared body if IRQ is still high. Normal acquisition retains its fresh-edge
requirement.

If erase entry timed out before any image transfer, append
`--resume-after-erase /path/to/failed-update-log` to the write command. The tool
checks the previous log's target, exact image hashes and final erase request,
then requires a live `0xff / 06 fe` (version query unsupported, -2) response
before sending the RAM updater. Normal firmware or any other error prevents
transfer. Resume sends neither STOP nor another erase. It cannot resume or
replay an image after transfer has begun.

This experimental profile does not imply official Sony distribution or
support for the externally supplied firmware.
