# I2C RTCM recording

English | [日本語](ja/rtcm.md)

[README](../README.md) · [Setup](setup.md) · [Firmware update](firmware-update.md)

The standalone tools capture RTCM observations over I2C. They do not add an RTCM
API to `CXD56xx` or the installed CLI. Spresense GNSS Add-on Board v00.144
supports RTCM collection; v00.103 rejects the RTCM notification command.
Other board/firmware combinations need separate validation. Receiving RTCM
does not by itself establish RTK/PPK accuracy.

## Prepare

Use Linux and the [I2C wiring and permissions](setup.md), including 1.8 V level
shifting, 400 kHz I2C and a verified IRQ line. Stop any other process using the
same receiver. Run from a checkout or unpacked source distribution with:

```sh
sudo apt install build-essential libgpiod-dev gpiod screen
```

libgpiod 2.0+ is required. Python uses only the standard library; the normal
Python extension does not need to be installed. The Python helper compiles
`examples/i2c_rtcm_probe.c` against the driver source into the log directory,
using a separate 4096-byte notification buffer.

## Record inside screen

```sh
./scripts/gnss-record --duration 600
```

Without `--duration`, recording continues until interrupted. The launcher
starts or reattaches to the `gnss-rtcm` screen session. Detach with **Ctrl+A,
then D**; reconnect by running it again. Stop with **Ctrl+C inside screen**
to allow cleanup and generation of the result files.

Use `./scripts/gnss-record --detach --duration 600` to start without a terminal,
`--foreground` inside an existing screen, or `status` to show the session and
latest log summary. Existing sessions are reattached rather than restarted;
new capture options take effect when starting a new recording.

The launcher detects the chip containing GPIO17 by line name. Override it with
`GNSS_GPIO_CHIP` and `GNSS_IRQ_LINE` after checking `gpioinfo`. The I2C defaults
are `/dev/i2c-1` and `0x24`, overridable with `GNSS_I2C_DEVICE` and
`GNSS_I2C_ADDRESS`. `GNSS_PYTHON` selects Python and `GNSS_SCREEN_SESSION`
selects the screen session name.

## Short capture without screen

```sh
python3 examples/i2c_rtcm_test.py \
  --gpio-chip /dev/gpiochip0 --irq-line 17 --duration 30 \
  --rtcm-mask 0x07102202 --gnss-mask 0x3fef
```

Replace the GPIO chip and line with the values verified for your hardware.
`--i2c-device` and `--address` override the bus/address; `--continuous` replaces
the time limit. The direct helper's default RTCM mask is `0x07f22222`; the screen
launcher explicitly uses the Sony sample mask `0x07102202` and signal selection
`0x3fef`. Supply those flags to reproduce the launcher's settings directly.

The probe queries firmware, stops positioning, configures RTCM and notifications,
then starts positioning. At normal or signal-triggered shutdown it attempts to
stop positioning, disable RTCM and restore the normal notification list. It does
not update firmware or save settings to backup storage. `--gnss-mask` changes
the volatile signal selection; its previous value is not restored. The normal
driver reapplies its own fixed selection on its next start.

## Logs and interpretation

The direct helper defaults to `$XDG_STATE_HOME/rpi-cxd56xx-gnss/rtcm-<UTC>/`
or `~/.local/state/rpi-cxd56xx-gnss/rtcm-<UTC>/`. The screen launcher uses
`recordings/` below the same state root and maintains a `latest` link. Override
with `--log-dir`, pointing to a new directory outside any Git checkout.

| File | Contents |
| --- | --- |
| `notifications.bin` | Reconstructed, checksum-verified received packets; may contain positions |
| `events.jsonl` | Command replies, capture status and cleanup acknowledgements |
| `observations.rtcm3` | Extracted frames passing CRC24Q validation |
| `rtcm-stream.bin`, `fragment-metadata.json` | Reassembled bytes and notification framing metadata |
| `summary.json` | Frame/message counts, CRC/size errors and cleanup status |
| `commands.json`, `build.log`, `driver.stderr.log` | Invocation, compilation and driver diagnostics |

Logs can contain positions and local paths. Keep them outside version control.
The decoder handles the measured three-byte Spresense v144 envelope and the
five-byte REYAX envelope. CRC checks do not detect every missing complete frame
or establish measurement quality; inspect continuity and the observations too.
Check that `normal_notifications_restored` is true after stopping.

Exit codes are `0` for a completed valid capture, `1` for capture/validation or
cleanup failure, and `2` for no valid RTCM frames (or invalid arguments).
A clean Ctrl+C/SIGTERM shutdown returns `130`/`143`; a cleanup failure still
returns `1`. A forced kill can prevent cleanup and leave incomplete logs.
