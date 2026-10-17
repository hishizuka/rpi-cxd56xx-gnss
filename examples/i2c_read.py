# SPDX-License-Identifier: Apache-2.0
"""Read position snapshots from a Sony GNSS receiver over I2C."""

import errno
import signal
import time

from cxd56xx_gnss import CXD56xx


def main():
    # The driver sends the binary initialization commands in its worker thread.
    gps = None

    def request_stop(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, request_stop)
    try:
        gps = CXD56xx()
        while True:
            result = gps.peek()
            if result == -errno.EAGAIN:
                print("Waiting for the first notification...")
            elif result != 0:
                raise OSError(-result, "GNSS read failed")
            else:
                print(
                    f"time={gps.timestamp} lat={gps.lat} lon={gps.lon} "
                    f"alt={gps.alt} speed={gps.speed} track={gps.track} "
                    f"mode={gps.mode} status={gps.status} "
                    f"sat={gps.used_sats}/{gps.total_sats}"
                )
            time.sleep(1)
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
