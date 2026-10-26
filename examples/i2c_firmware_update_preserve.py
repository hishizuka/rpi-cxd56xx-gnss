# SPDX-License-Identifier: Apache-2.0
"""Update audited CXD5610 code images over I2C without CFG writes or erase.

The default action is an offline plan. Firmware files are not distributed.
The v201 profile is experimental on Spresense; keep the board's own CFG.
"""

import argparse
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time

from _firmware_common import (
    ReceiverCommandError, image_summary, injection_frames, is_injection_frame,
    load_image_files, packet_parts, validate_code_images, wire_command_event,
)
from i2c_firmware_update import IMAGES, I2CTransport, read_version
from i2c_window_test import create_log_dir


PROFILES = {
    "v144": ("v00.144", IMAGES[:4]),
    "v201": ("v00.201", (
        ("cxd5610_updater.efpk", 30656, "8aa0825286b545d41b1c37d36f096b81a2173a4175291c5c6b2aac47ce976046", None),
        ("cxd5610_sdk.efpk", 707632, "45606eeac69d9195cef9fc8a1b28876ff66c147bc4b02127ef9cdff3f954af6f", 0),
        ("cxd5610_app.efpk", 266640, "a48af7d8e0e82fd954be525d6d7c1a83c7b85db30bb7958fed513f36cce59593", 1),
        ("cxd5610_lib.efpk", 1088, "cedaebe58fa8c702caae5cb6fb42449d1db20adbea6bafdbf30a82589144fd5c", 2),
    )),
}
SETTING_QUERIES = (0x33, 0x38, 0x3B, 0x3E, 0x41)


def validate_images(images, profile):
    validate_code_images(images, PROFILES[profile][1])


def load_images(directory, profile):
    version, specs = PROFILES[profile]
    allowed = {spec[0] for spec in specs} | {"FW_VERSION"}
    if any(path.name not in allowed for path in directory.iterdir()):
        raise ValueError("Image directory must not include CFG or unexpected files")
    version_file = directory / "FW_VERSION"
    if version_file.exists() and version_file.read_text().strip() != version:
        raise ValueError("FW_VERSION differs from the selected profile")
    return load_image_files(directory, specs)


def allowed_command(opcode, payload):
    if not payload and opcode in (0x02, 0x06, 0x31, *SETTING_QUERIES):
        return True
    if opcode == 0x00:
        return payload in (b"\xff", b"\x01")
    if opcode == 0x09:
        return payload in (b"\x00\x00\x00", b"\x01\x00\x00", b"\x02\x00\x00")
    if opcode == 0x01:
        return is_injection_frame(payload)
    return False


class CommandReplyError(ValueError):
    def __init__(self, opcode, response):
        self.opcode, self.response = opcode, response
        super().__init__(f"Command 0x{opcode:02x} rejected: {response.hex()}")


class CommandWriteError(OSError):
    """A command write failed before any response read; retry only at entry."""


class PreservingI2CTransport(I2CTransport):
    def __init__(self, device, address, chip, irq, emit):
        self.emit = emit
        super().__init__(device, address, chip, irq)

    def exchange(self, opcode, payload, timeout):
        # Enforce the policy at the last point before any I2C write.
        if not allowed_command(opcode, payload):
            raise ValueError("Erase, CFG, settings writes or unexpected command is forbidden")
        packet = b"".join(packet_parts(opcode, payload))
        self.emit(wire_command_event(opcode, payload, packet))
        try:
            written = os.write(self.fd, packet)
        except OSError as exc:
            if exc.errno in (errno.ENXIO, errno.EIO, getattr(errno, "EREMOTEIO", 121)):
                raise CommandWriteError(exc.errno, "I2C command write failed") from exc
            raise
        if written != len(packet):
            raise OSError("Short I2C command; no retry, transfer state unknown")
        return self._read_response(opcode, time.monotonic() + timeout, self.emit)


def query_settings(transport):
    result = {"version": read_version(transport), "settings": []}
    for opcode in SETTING_QUERIES:
        try:
            response = transport.exchange(opcode, b"", 5)
        except ReceiverCommandError as exc:
            if opcode in (0x33, 0x3E):
                raise
            result["settings"].append({"opcode": opcode, "error": str(exc)})
        else:
            result["settings"].append({"opcode": opcode, "reply_hex": response.hex()})
    return result


def run_update(transport, images, profile, expected_version, emit, settle=time.sleep,
               reboot_before_force=False, reset_wait=0):
    if not 0 <= reset_wait <= 300 or (reset_wait and reboot_before_force):
        raise ValueError("Choose one entry strategy; reset wait must be within 0..300 seconds")
    # Revalidate even when this function is called independently of the CLI.
    validate_images(images, profile)
    target = PROFILES[profile][0]
    before = read_version(transport)
    emit({"event": "version_before", "version": before})
    if before == target:
        emit({"event": "already_updated", "version": before})
        return before
    if before != expected_version:
        raise ValueError(f"Expected {expected_version}, received {before}; no update started")

    def checked(opcode, payload=b"", timeout=5, expected=b"\0", **details):
        if not allowed_command(opcode, payload):
            raise ValueError("Forbidden command in update sequence")
        emit({"event": "command_sent", "opcode": opcode,
              "operand_hex": payload.hex() if opcode != 1 else None, **details})
        response = transport.exchange(opcode, payload, timeout)
        emit({"event": "response", "opcode": opcode, "response_hex": response.hex(), **details})
        if response != expected:
            raise CommandReplyError(opcode, response)

    checked(0x31)
    if reboot_before_force:
        # OPR=1 boots the existing eMRAM firmware; OPR=0 erases and is forbidden.
        # This explicit entry strategy is never an automatic failure fallback.
        checked(0x00, b"\x01", expected=b"\x01", phase="non_erasing_reboot")
    if reset_wait:
        emit({"event": "waiting_for_physical_reset", "timeout_s": reset_wait})
        deadline = time.monotonic() + reset_wait
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Physical-reset ForceBoot window expired; no image sent")
            try:
                checked(0x00, b"\xff", phase="force_entry")
            except ReceiverCommandError as exc:
                if exc.opcode != 0 or exc.code not in (-2, -22):
                    raise
            except CommandWriteError:
                # Reset can produce NACK or EIO; retry only ForceBoot at entry.
                # Partial reads, short writes and IRQ timeouts are not retried.
                pass
            except CommandReplyError as exc:
                # Allow a complete normal-SDK rejection or startup indication.
                # The checked helper records it; never retry other responses.
                if exc.opcode != 0 or exc.response not in (b"\xea", b"\x01", b"\x02"):
                    raise
            else:
                break
            settle(0.01)
    else:
        checked(0x00, b"\xff", phase="force_entry")
    for name, data, kind in images:
        emit({"event": "image_started", "image": name, "kind": kind,
              "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        for payload in injection_frames(data):
            total, serial = struct.unpack_from("<HH", payload)
            checked(0x01, payload, timeout=10, image=name, frame=serial, total_frames=total)
        if kind is None:
            checked(0x02, timeout=7, phase="execute_ram_updater")
            # A force-entry ACK alone may have been a startup notification.
            # Never write code if the normal firmware still answers VER.
            try:
                normal_version = read_version(transport)
            except TimeoutError:
                emit({"event": "ram_probe", "normal_version_returned": False, "reason": "version_timeout"})
            except ReceiverCommandError as exc:
                if exc.opcode != 0x06 or exc.code != -2:
                    raise
                emit({"event": "ram_probe", "normal_version_returned": False, "reason": "version_not_implemented"})
            else:
                raise RuntimeError(f"Normal firmware {normal_version} still responds after RAM execution; no SDK/APP/LIB write sent")
        else:
            checked(0x09, bytes((kind, 0, 0)), timeout=30, phase="write_image", image=name)
        emit({"event": "image_completed", "image": name})
    try:
        checked(0x00, b"\x01", expected=b"\x01", phase="boot")
    except TimeoutError:
        emit({"event": "boot_ack_missing", "independent_version_required": True})
    settle(1)
    after = read_version(transport)
    emit({"event": "version_after", "version": after})
    if after != target:
        raise ValueError(f"Expected target {target}, received {after}")
    return after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--image-dir", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", action="store_true")
    action.add_argument("--query", action="store_true", help="Read version and documented settings only")
    parser.add_argument("--expected-version", default="v00.144")
    entry = parser.add_mutually_exclusive_group()
    entry.add_argument("--reboot-before-force", action="store_true",
                        help="Explicitly reboot from existing eMRAM firmware before ForceBoot; never erase")
    entry.add_argument("--wait-for-reset", type=float, default=0,
                       help="Poll ForceBoot for up to 300 seconds while the GNSS alone is physically reset")
    parser.add_argument("--gpio-chip")
    parser.add_argument("--irq-line", type=int, default=17)
    parser.add_argument("--i2c-device", default="/dev/i2c-1")
    parser.add_argument("--address", type=lambda s: int(s, 0), default=0x24)
    parser.add_argument("--log-dir", type=Path)
    args = parser.parse_args()
    if not args.query and args.image_dir is None:
        parser.error("--image-dir is required for offline planning or writing")
    if (args.write or args.query) and (sys.platform != "linux" or not args.gpio_chip):
        parser.error("Device access requires Linux and a verified --gpio-chip")
    if not 0 <= args.address <= 0x7F or args.irq_line < 0:
        parser.error("Invalid I2C address or IRQ line")
    if not 0 <= args.wait_for_reset <= 300:
        parser.error("--wait-for-reset must be within 0..300 seconds")
    os.umask(0o077)
    images = [] if args.query else load_images(args.image_dir, args.profile)
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home()/".local/state"))
    default = state/"rpi-cxd56xx-gnss"/datetime.now(timezone.utc).strftime("fw-preserve-%Y%m%dT%H%M%S.%fZ")
    log_dir = create_log_dir(args.log_dir or default)
    summary = {"started_utc": datetime.now(timezone.utc).isoformat(), "argv": sys.argv,
               "mode": "write" if args.write else "query" if args.query else "offline_plan",
               "profile": args.profile, "target_version": PROFILES[args.profile][0],
               "config_write_permitted": False, "erase_permitted": False,
               "device_opened": False, "completed": False, "errors": [],
               "images": image_summary(images),
               "planned_program_operands": ["000000", "010000", "020000"] if images else []}
    transport = None
    try:
        with (log_dir/"events.jsonl").open("x", buffering=1) as output:
            def emit(event):
                event = {"utc": datetime.now(timezone.utc).isoformat(), **event}
                output.write(json.dumps(event)+"\n")
                if event["event"] in ("image_started", "image_completed", "version_before", "version_after", "already_updated", "waiting_for_physical_reset"):
                    os.fsync(output.fileno())
                    print(json.dumps(event), flush=True)
                elif event.get("phase") == "write_image":
                    os.fsync(output.fileno())
                summary["last_event"] = event
            if args.write or args.query:
                transport = PreservingI2CTransport(args.i2c_device, args.address, args.gpio_chip, args.irq_line, emit)
                summary["device_opened"] = True
                if args.query:
                    summary["settings_snapshot"] = query_settings(transport)
                    summary["verified_version"] = summary["settings_snapshot"]["version"]
                else:
                    summary["verified_version"] = run_update(
                        transport, images, args.profile, args.expected_version, emit,
                        reboot_before_force=args.reboot_before_force,
                        reset_wait=args.wait_for_reset)
            summary["completed"] = True
    except (Exception, KeyboardInterrupt) as exc:
        summary["errors"].append({"type": type(exc).__name__, "message": str(exc)})
        print(f"Stopped without automatic retry, erase or reset: {exc}", file=sys.stderr, flush=True)
    finally:
        if transport is not None:
            transport.close()
        summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
        (log_dir/"summary.json").write_text(json.dumps(summary, indent=2)+"\n")
    print(json.dumps(summary), flush=True)
    return 0 if summary["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
