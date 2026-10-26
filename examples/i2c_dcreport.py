# SPDX-License-Identifier: Apache-2.0
"""Read raw QZSS DC reports over I2C, optionally decoding them with azarashi."""

import argparse
import signal
import time
from datetime import datetime, timezone

from cxd56xx_gnss import CXD56xx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--decode", action="store_true", help="Decode with optional azarashi >= 0.17.1"
    )
    parser.add_argument(
        "--reconstruct-crc", action="store_true",
        help="With --decode, reconstruct CRC in a copy for unverified parsing",
    )
    args = parser.parse_args()
    if args.reconstruct_crc and not args.decode:
        parser.error("--reconstruct-crc requires --decode")
    if args.decode:
        import azarashi

    def request_stop(signum, frame):
        raise KeyboardInterrupt

    gps = None
    previous = signal.signal(signal.SIGTERM, request_stop)
    try:
        gps = CXD56xx()
        gps.wait_ready()
        dropped = 0
        print("Waiting for QZSS L1S DC reports; Ctrl+C to stop.", flush=True)
        while True:
            # DC reports do not depend on peek() or a valid position fix.
            for message in gps.read_dcreports():
                received_at = datetime.fromtimestamp(message.received_at, timezone.utc)
                print(
                    f"{received_at.isoformat()} satellite_id={message.satellite_id} "
                    f"data={message.data.hex().upper()}",
                    flush=True,
                )
                if args.decode:
                    try:
                        report = azarashi.decode(
                            message.to_azarashi_packet(reconstruct_crc=args.reconstruct_crc),
                            "net",
                            timestamp=received_at,
                        )
                    except azarashi.AzarashiReadOn as exc:
                        print(f"[{type(exc).__name__}] {exc}", flush=True)
                    else:
                        print(report, flush=True)
            current_dropped = gps.dcreports_dropped
            if current_dropped != dropped:
                print(f"Queue overflow: {current_dropped} reports dropped in total", flush=True)
                dropped = current_dropped
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nStopping GNSS.")
    finally:
        try:
            if gps is not None:
                gps.close()
        finally:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
