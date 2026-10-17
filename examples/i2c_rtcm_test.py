# SPDX-License-Identifier: Apache-2.0
"""Capture experimental I2C RTCM notifications in a private log directory."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from i2c_window_test import create_log_dir, positive_number


def crc24q(data):
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc


def decode_capture(data):
    offset = 0
    opcodes = Counter()
    stream = bytearray()
    fragments = []
    while offset < len(data):
        header = data[offset:offset + 5]
        if len(header) != 5 or header[0] != 0x7F or sum(header[:4]) % 256 != header[4]:
            raise ValueError(f"Invalid captured header at byte {offset}")
        length = int.from_bytes(header[1:3], "little")
        end = offset + 5 + length + bool(length)
        if end > len(data):
            raise ValueError(f"Truncated captured packet at byte {offset}")
        payload = data[offset + 5:offset + 5 + length]
        if length and sum(payload) % 256 != data[end - 1]:
            raise ValueError(f"Invalid captured payload checksum at byte {offset}")
        opc = header[3]
        opcodes[opc] += 1
        if opc == 0x84:
            if length < 3:
                raise ValueError("RTCM notification has no fragment header")
            size = int.from_bytes(payload[1:3], "little")
            # Spresense v00.144 emits [valid/version, size_le16, RTCM].
            # REYAX's documented envelope additionally carries two frame bytes.
            # Select by the declared size, without scanning for a preamble.
            compact = payload[0] == 0x80 and size == length - 3
            if not compact and length < 5:
                raise ValueError("RTCM notification has no fragment counters")
            fragment = payload[3:] if compact else payload[5:]
            fragments.append({
                "declared_size": size, "actual_size": len(fragment),
                "format": "spresense-v144" if compact else "reyax-fragmented",
                "header_bytes": 3 if compact else 5,
                "total_frames": None if compact else payload[3],
                "serial": None if compact else payload[4],
            })
            # Keep fragment order and validate the reconstructed RTCM framing below.
            stream.extend(fragment)
        offset = end
    return bytes(stream), {f"0x{opc:02x}": count for opc, count in sorted(opcodes.items())}, fragments


def split_rtcm(stream):
    frames = []
    offset = 0
    crc_errors = 0
    discarded = 0
    while offset + 3 <= len(stream):
        if stream[offset] != 0xD3 or stream[offset + 1] & 0xFC:
            discarded += 1
            offset += 1
            continue
        length = ((stream[offset + 1] & 3) << 8) | stream[offset + 2]
        end = offset + 3 + length + 3
        if end > len(stream):
            break
        frame = stream[offset:end]
        if crc24q(frame[:-3]) != int.from_bytes(frame[-3:], "big"):
            crc_errors += 1
            discarded += 1
            offset += 1
            continue
        if length < 2:
            raise ValueError("RTCM frame is too short for a message type")
        frames.append(frame)
        offset = end
    return frames, {
        "crc_errors": crc_errors, "discarded_bytes": discarded,
        "trailing_bytes": len(stream) - offset,
    }


def analyze(log_dir):
    stream, opcodes, fragments = decode_capture((log_dir / "notifications.bin").read_bytes())
    (log_dir / "rtcm-stream.bin").write_bytes(stream)
    frames, validation = split_rtcm(stream)
    (log_dir / "observations.rtcm3").write_bytes(b"".join(frames))
    types = Counter((frame[3] << 4) | (frame[4] >> 4) for frame in frames)
    (log_dir / "fragment-metadata.json").write_text(json.dumps(fragments, indent=2) + "\n")
    return {
        "opcodes": opcodes, "rtcm_notifications": len(fragments),
        "rtcm_stream_bytes": len(stream), "valid_rtcm_frames": len(frames),
        "rtcm_message_counts": dict(sorted(types.items())),
        "fragment_size_mismatches": sum(f["declared_size"] != f["actual_size"] for f in fragments),
        "notification_formats": dict(Counter(f["format"] for f in fragments)),
        **validation,
    }


def summarize_events(events):
    summary = {
        "command_errors": [event for event in events if event.get("event") == "command" and event.get("result", 0) != 0],
        "rtcm_notification_accepted": any(
            event.get("opc") == 0x34 and event.get("result") == 0
            and 0x84 in bytes.fromhex(event.get("request_hex", ""))
            for event in events
        ),
        "capture_started": any(event.get("event") == "capture" for event in events),
        "normal_notifications_restored": any(
            event.get("event") == "cleanup"
            and all(event.get(key) == 0 for key in ("stop", "disable_rtcm", "normal_notify"))
            for event in events
        ),
    }
    for event in events:
        if event.get("opc") == 0x06 and event.get("result") == 0:
            summary["firmware_reply_ascii"] = bytes.fromhex(event["payload_hex"]).decode("ascii", errors="replace").strip("\x00\r\n")
    return summary


def parse_rtcm_mask(value):
    try:
        mask = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a decimal or hexadecimal integer") from exc
    if not 0 <= mask <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("must fit in an unsigned 32-bit mask")
    return mask


def parse_gnss_mask(value):
    mask = parse_rtcm_mask(value)
    if not mask or mask & ~0x3FEF:
        raise argparse.ArgumentTypeError("must select supported signals only (mask 0x3fef)")
    return mask


def capture_exit_code(summary):
    invalid = any(summary.get(key) for key in (
        "crc_errors", "discarded_bytes", "trailing_bytes", "fragment_size_mismatches",
        "errors", "forced_kill",
    ))
    if invalid or not summary.get("normal_notifications_restored"):
        return 1
    if summary.get("returncode") == 130 and summary.get("stop_signal"):
        return 128 + summary["stop_signal"]
    if summary.get("returncode") != 0:
        return 1
    return 0 if summary.get("valid_rtcm_frames") else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpio-chip", required=True, help="Chip verified with gpiodetect/gpioinfo")
    parser.add_argument("--irq-line", type=int, default=17)
    parser.add_argument("--i2c-device", default="/dev/i2c-1")
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x24)
    timing = parser.add_mutually_exclusive_group()
    timing.add_argument("--duration", type=positive_number, default=30)
    timing.add_argument("--continuous", action="store_true", help="Collect until Ctrl+C or SIGTERM")
    parser.add_argument("--gnss-mask", type=parse_gnss_mask, help="Optional volatile signal selection; all supported signals: 0x3fef")
    parser.add_argument(
        "--rtcm-mask", type=parse_rtcm_mask, default=0x07F22222,
        help="Output-type mask; Sony GPS/QZSS/Galileo sample uses 0x07102202",
    )
    parser.add_argument("--log-dir", type=Path)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Capture requires Linux and libgpiod >= 2.0 development files")
    if args.irq_line < 0 or not 0 <= args.address <= 0x7F:
        parser.error("Invalid IRQ line or I2C address")
    os.umask(0o077)
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    path = args.log_dir or state / "rpi-cxd56xx-gnss" / datetime.now(timezone.utc).strftime("rtcm-%Y%m%dT%H%M%S.%fZ")
    try:
        log_dir = create_log_dir(path)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Logs: {log_dir}", flush=True)
    source = Path(__file__).with_name("i2c_rtcm_probe.c")
    compile_argv = [
        "cc", "-O2", "-pthread",
        f"-DCXD5610_GPIO_CHIP_DEV={json.dumps(args.gpio_chip)}",
        f"-DCXD5610_INT_GPIO={args.irq_line}",
        f"-DCXD5610_I2C_DEV={json.dumps(args.i2c_device)}",
        f"-DCXD5610_I2C_ADDR={args.address}",
        str(source), "-o", str(log_dir / "probe"), "-lgpiod", "-lm",
    ]
    argv = [str(log_dir / "probe"), str(log_dir / "notifications.bin"), "0" if args.continuous else str(args.duration), hex(args.rtcm_mask)]
    if args.gnss_mask is not None:
        argv.append(hex(args.gnss_mask))
    summary = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "compile_argv": compile_argv, "argv": argv,
        "requested_duration_s": None if args.continuous else args.duration,
        "continuous": args.continuous, "gnss_mask": args.gnss_mask,
        "rtcm_mask": args.rtcm_mask,
        "rtcm_mask_hex": args.rtcm_mask.to_bytes(4, "little").hex(), "errors": [],
        "firmware_updated": False, "backup_save_requested": False,
    }
    (log_dir / "commands.json").write_text(json.dumps(summary, indent=2) + "\n")
    try:
        with (log_dir / "build.log").open("x") as output:
            subprocess.run(compile_argv, stdout=output, stderr=subprocess.STDOUT, check=True)
        with (log_dir / "events.jsonl").open("x") as output, (log_dir / "driver.stderr.log").open("x") as errors:
            process = None
            stopping = False

            def request_stop(signum, frame):
                summary.setdefault("stop_signal", signum)
                # A request during Popen is forwarded once the child is known.
                if process is not None and not stopping:
                    raise KeyboardInterrupt

            old_handlers = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                process = subprocess.Popen(argv, stdout=output, stderr=errors)
                if summary.get("stop_signal"):
                    raise KeyboardInterrupt
                summary["returncode"] = process.wait(timeout=None if args.continuous else args.duration + 45)
            except (KeyboardInterrupt, subprocess.TimeoutExpired) as exc:
                stopping = True
                summary["stop_reason"] = type(exc).__name__
                if isinstance(exc, KeyboardInterrupt):
                    summary.setdefault("stop_signal", int(signal.SIGINT))
                process.send_signal(summary.get("stop_signal", signal.SIGINT))
                try:
                    summary["returncode"] = process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    summary["returncode"] = process.wait()
                    summary["forced_kill"] = True
            finally:
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
        summary.update(analyze(log_dir))
        events = [json.loads(line) for line in (log_dir / "events.jsonl").read_text().splitlines()]
        summary["events"] = events
        summary.update(summarize_events(events))
        summary["driver_stderr_bytes"] = (log_dir / "driver.stderr.log").stat().st_size
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        summary["errors"].append({"type": type(exc).__name__, "message": str(exc)})
    summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
    (log_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({key: summary.get(key) for key in (
        "returncode", "firmware_reply_ascii", "rtcm_notifications", "valid_rtcm_frames",
        "rtcm_message_counts", "crc_errors", "discarded_bytes", "trailing_bytes", "errors",
    )}), flush=True)
    return capture_exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
