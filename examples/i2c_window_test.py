# SPDX-License-Identifier: Apache-2.0
"""Observe the existing I2C driver and save private, time-limited test logs."""

import argparse
import errno
import json
import math
import os
from pathlib import Path
import signal
import statistics
import sys
import time
from datetime import datetime, timezone


FIELDS = (
    "timestamp", "lat", "lon", "alt", "speed", "track", "mode", "status",
    "used_sats", "total_sats", "pdop", "hdop", "vdop",
)


def coordinates_in_range(sample):
    lat, lon = sample.get("lat"), sample.get("lon")
    return (
        isinstance(lat, (int, float)) and math.isfinite(lat)
        and isinstance(lon, (int, float)) and math.isfinite(lon)
        and -90 <= lat <= 90 and -180 <= lon <= 180
    )


def has_fix(sample):
    return (
        sample.get("mode") in (2, 3) and sample.get("status") in (1, 2)
        and coordinates_in_range(sample)
    )


def interval_stats(values):
    if not values:
        return None
    return {
        "count": len(values), "min": min(values),
        "median": statistics.median(values), "max": max(values),
    }


def create_log_dir(path):
    path = path.expanduser().resolve()
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("Log directory must be outside a Git checkout")
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    return path


def observe(receiver_factory, log_dir, duration, interval, board_model):
    started = time.monotonic()
    stopped = False
    receiver = None
    previous_timestamp = None
    last_update = None
    device_intervals = []
    host_intervals = []
    summary = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "board_model_user_reported": board_model,
        "requested_duration_s": duration, "poll_interval_s": interval,
        "ttff_basis": "seconds since this driver initialization; not a cold-start TTFF",
        "satellite_count_basis": "used_sats counts tracking flags, not solution usage",
        "communication_success": False, "fix_success": False,
        "successful_peeks": 0, "waiting_peeks": 0, "read_errors": 0,
        "timestamp_updates": 0, "timestamp_regressions": 0,
        "first_notification_s": None, "first_fix_s": None,
        "max_tracked_sats": None, "max_total_sats": None,
        "fix_polls": 0, "invalid_coordinate_polls_with_fix": 0,
        "completed": False, "close_success": None, "errors": [],
    }

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    old_handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with (log_dir / "samples.jsonl").open("x", buffering=1) as samples:
            receiver = receiver_factory()
            summary["initialized_s"] = time.monotonic() - started
            next_poll = time.monotonic()
            next_progress = 0
            while not stopped and time.monotonic() - started < duration:
                elapsed = time.monotonic() - started
                result = receiver.peek()
                row = {
                    "host_utc": datetime.now(timezone.utc).isoformat(),
                    "elapsed_s": elapsed, "result": result,
                }
                if result == -errno.EAGAIN:
                    summary["waiting_peeks"] += 1
                elif result != 0:
                    summary["read_errors"] += 1
                    row["error"] = os.strerror(abs(result))
                else:
                    row.update({field: getattr(receiver, field) for field in FIELDS})
                    summary["successful_peeks"] += 1
                    summary["communication_success"] = True
                    if summary["first_notification_s"] is None:
                        summary["first_notification_s"] = elapsed
                    ts = row["timestamp"]
                    changed = isinstance(ts, (int, float)) and math.isfinite(ts) and ts != previous_timestamp
                    row["timestamp_changed"] = changed
                    if changed:
                        summary["timestamp_updates"] += 1
                        if previous_timestamp is not None:
                            delta = ts - previous_timestamp
                            if delta > 0:
                                device_intervals.append(delta)
                            else:
                                summary["timestamp_regressions"] += 1
                        if last_update is not None:
                            host_intervals.append(elapsed - last_update)
                        previous_timestamp = ts
                        last_update = elapsed
                    # A cached snapshot must not establish a new fix after updates stop.
                    fresh = last_update is not None and elapsed - last_update <= max(5, interval * 3)
                    row["fresh_timestamp"] = fresh
                    row["coordinates_in_range"] = coordinates_in_range(row)
                    row["fix"] = has_fix(row) and fresh
                    if row["fix"]:
                        summary["fix_polls"] += 1
                        summary["fix_success"] = True
                        if summary["first_fix_s"] is None:
                            summary["first_fix_s"] = elapsed
                    if row["mode"] in (2, 3) and row["status"] in (1, 2) and not row["coordinates_in_range"]:
                        summary["invalid_coordinate_polls_with_fix"] += 1
                    for field, target in (("used_sats", "max_tracked_sats"), ("total_sats", "max_total_sats")):
                        value = row[field]
                        if value is not None:
                            old = summary[target]
                            summary[target] = value if old is None else max(old, value)
                    summary["last_sample"] = row
                samples.write(json.dumps(row, allow_nan=False) + "\n")
                if elapsed >= next_progress:
                    print(
                        f"elapsed={elapsed:.1f}s communication={summary['communication_success']} "
                        f"fix={row.get('fix', False)} mode={row.get('mode')} status={row.get('status')} "
                        f"sat={row.get('used_sats')}/{row.get('total_sats')} "
                        f"updates={summary['timestamp_updates']} errors={summary['read_errors']}",
                        flush=True,
                    )
                    next_progress = elapsed + 30
                next_poll += interval
                delay = min(next_poll, started + duration) - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_poll = time.monotonic()
            summary["observation_duration_s"] = time.monotonic() - started
            summary["completed"] = not stopped
    except Exception as exc:
        summary["errors"].append({"type": type(exc).__name__, "message": str(exc)})
    finally:
        observation_end = time.monotonic() - started
        summary.setdefault("observation_duration_s", observation_end)
        summary["last_timestamp_update_age_s"] = None if last_update is None else observation_end - last_update
        summary["live_updates_observed"] = summary["timestamp_updates"] >= 2
        summary["device_update_interval_s"] = interval_stats(device_intervals)
        summary["host_update_interval_s"] = interval_stats(host_intervals)
        close_started = time.monotonic()
        if receiver is not None:
            try:
                close = getattr(receiver, "close", None)
                if callable(close):
                    close()
                    summary["close_success"] = True
            except Exception as exc:
                summary["close_success"] = False
                summary["errors"].append({"phase": "close", "type": type(exc).__name__, "message": str(exc)})
            finally:
                del receiver
        summary["close_duration_s"] = time.monotonic() - close_started
        summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
        summary["interrupted"] = stopped
        with (log_dir / "summary.json").open("x") as output:
            json.dump(summary, output, indent=2, allow_nan=False)
            output.write("\n")
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    return summary


def positive_number(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=positive_number, default=600)
    parser.add_argument("--interval", type=positive_number, default=0.2)
    parser.add_argument("--board-model", default="unconfirmed")
    parser.add_argument("--log-dir", type=Path, help="New directory outside any Git checkout")
    args = parser.parse_args()
    os.umask(0o077)
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    path = args.log_dir or state / "rpi-cxd56xx-gnss" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    try:
        log_dir = create_log_dir(path)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Logs: {log_dir}", flush=True)
    # Redirect the file descriptor so messages from the C worker are captured too.
    with (log_dir / "driver.stderr.log").open("x") as driver_log:
        saved_stderr = os.dup(2)
        try:
            sys.stderr.flush()
            os.dup2(driver_log.fileno(), 2)
            def receiver_factory():
                from cxd56xx_gnss import CXD56xx
                return CXD56xx()

            summary = observe(receiver_factory, log_dir, args.duration, args.interval, args.board_model)
        finally:
            sys.stderr.flush()
            os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
    print(
        f"completed={summary['completed']} communication={summary['communication_success']} "
        f"fix={summary['fix_success']} first_fix_s={summary['first_fix_s']} "
        f"read_errors={summary['read_errors']} exceptions={len(summary['errors'])}",
        flush=True,
    )
    # Lack of a fix is a valid observation, separate from a communication failure.
    if summary["errors"] or summary["read_errors"]:
        return 1
    if summary["interrupted"]:
        return 130
    if not summary["communication_success"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
