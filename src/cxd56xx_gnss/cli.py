# SPDX-License-Identifier: Apache-2.0
"""Display GNSS snapshots using the installed extension."""

import errno
import signal
import sys
import time
from datetime import datetime, timezone

from . import CXD56xx


def main():
    gps = None

    def request_stop(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, request_stop)
    try:
        try:
            gps = CXD56xx()
        except OSError as exc:
            print(f"[INIT] failed (errno={exc.errno}): {exc}", file=sys.stderr)
            return 1

        print("[INIT] CXD56xx initialized. Reading every 1s; Ctrl+C to stop.")
        while True:
            ret = gps.peek()
            if ret == -errno.EAGAIN:
                print("[DATA] waiting for the first notification...")
            elif ret < 0:
                print(f"[WARN] peek returned {ret}", file=sys.stderr)
            else:
                ts = gps.timestamp
                utc = (
                    datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    if ts is not None
                    else "-"
                )
                print(
                    f"time={utc} lat={gps.lat} lon={gps.lon} alt={gps.alt} "
                    f"speed={gps.speed} track={gps.track} "
                    f"mode={gps.mode} status={gps.status} "
                    f"sat={gps.used_sats}/{gps.total_sats} "
                    f"DOP={gps.pdop}/{gps.hdop}/{gps.vdop}"
                )
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[EXIT] stop requested.")
    finally:
        try:
            if gps is not None:
                gps.close()
        finally:
            signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
