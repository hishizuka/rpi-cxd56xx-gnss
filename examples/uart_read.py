# SPDX-License-Identifier: Apache-2.0
"""Initialize a Sony GNSS receiver over UART and print its NMEA output."""

import argparse
import sys
import time

import serial


def send_command(port, command):
    """Send an ASCII command and wait up to five seconds for its reply."""
    print(f"> {command}", flush=True)
    port.write((command + "\r\n").encode("ascii"))
    prefix = f"[{command.split()[0][1:]}]"
    deadline = time.monotonic() + 5.0
    pending = bytearray()
    while time.monotonic() < deadline:
        pending.extend(port.readline())
        if not pending.endswith(b"\n"):
            continue
        line = pending.decode("ascii", errors="replace").strip()
        pending.clear()
        print(line, flush=True)
        if line.startswith(prefix):
            result = line[len(prefix) :].strip()
            if result == "Done":
                return
            if result.startswith("Err"):
                raise RuntimeError(line)
    raise TimeoutError(f"No reply to {command}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", nargs="?", default="/dev/ttyS0")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument(
        "--gns",
        type=lambda value: int(value, 0),
        required=True,
        help="Satellite mask: 47 for L1 GPS/GLONASS/SBAS/QZSS, 16367 for L1+L5",
    )
    parser.add_argument(
        "--init-only", action="store_true", help="Exit after starting positioning"
    )
    args = parser.parse_args()
    if args.gns <= 0:
        parser.error("--gns must be a positive integer (decimal or 0x hexadecimal)")

    try:
        with serial.Serial(
            args.port, baudrate=args.baudrate, timeout=0.2, write_timeout=5
        ) as port:
            for command in ("@GSTP", "@GGNS", f"@GNS {args.gns}", "@GGNS", "@GSR"):
                send_command(port, command)
            if not args.init_only:
                print("Reading NMEA; Ctrl+C to close the UART.", flush=True)
                while True:
                    sys.stdout.write(port.readline().decode("ascii", errors="replace"))
                    sys.stdout.flush()
    except KeyboardInterrupt:
        print("\nUART closed.")
    except (serial.SerialException, RuntimeError, TimeoutError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
