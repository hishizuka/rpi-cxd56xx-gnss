# SPDX-License-Identifier: Apache-2.0
"""Experimental RYS8839 UART update using v144 SDK/APP/LIB only.

Firmware compatibility with RYS8839 is unverified. No configuration image or
erase command is permitted. Firmware stays outside the repository. By default
this command only validates the package and writes an offline plan.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import time

from _firmware_common import (
    ReceiverCommandError, decode_header, injection_frames, is_injection_frame,
    packet_parts, validate_code_images, wire_command_event,
)
from i2c_firmware_update import IMAGES, TARGET_VERSION, load_package
from i2c_window_test import create_log_dir


def validate_images(images):
    # Do not merely filter an arbitrary caller's list: require the exact set.
    validate_code_images(images, IMAGES[:4])


def allowed_command(opcode, payload):
    if (opcode, payload) in ((0x06, b""), (0x31, b""),
                             (0x00, b"\xff"), (0x00, b"\x01"), (0x02, b"")):
        return True
    if opcode == 0x09:
        return payload in (b"\x00\x00\x00", b"\x01\x00\x00", b"\x02\x00\x00")
    if opcode == 0x01:
        return is_injection_frame(payload)
    return False


class UARTTransport:
    def __init__(self, port, baudrate, raw, emit):
        import serial

        self.serial = serial.Serial(port, baudrate, timeout=0.05,
                                    write_timeout=5, exclusive=True)
        self.raw = raw
        self.emit = emit
        self.buffer = bytearray()

    def close(self):
        self.serial.close()

    def ascii_command(self, name, timeout=5):
        if name not in ("VER", "GSTP"):
            raise ValueError("Unexpected ASCII command is forbidden")
        packet = ("@" + name + "\r\n").encode("ascii")
        self.emit({"event": "ascii_command_sent", "command": "@" + name})
        if self.serial.write(packet) != len(packet):
            raise OSError("Short UART write")
        self.serial.flush()
        data = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self.serial.read(self.serial.in_waiting or 1)
            self.raw.write(chunk)
            self.raw.flush()
            data.extend(chunk)
            if ("[" + name + "] Done").encode() in data:
                self.emit({"event": "ascii_response", "command": "@" + name,
                           "response_hex": data.hex()})
                return bytes(data)
            if ("[" + name + "] Err").encode() in data:
                raise ValueError("ASCII command rejected: " + name)
        raise TimeoutError("ASCII response timed out: " + name)

    def read_version(self):
        response = self.ascii_command("VER")
        match = re.search(rb"v[0-9]+\.[0-9]+", response)
        if match is None:
            raise ValueError("Missing ASCII firmware version")
        return match[0].decode("ascii")

    def stop(self):
        self.ascii_command("GSTP")

    def force_boot(self, timeout):
        self.emit({"event": "waiting_for_force_boot", "timeout_s": timeout})
        deadline = time.monotonic() + timeout
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            try:
                response = self.exchange(0, b"\xff", min(0.05, deadline - time.monotonic()))
            except TimeoutError:
                continue
            except ReceiverCommandError as exc:
                # GNSS Monitor also keeps polling through boot-time command
                # errors. Only the read/entry phase may repeat a request.
                if exc.opcode == 0 and exc.code == -2:
                    self.emit({"event": "force_entry_not_ready", "attempt": attempts,
                               "error": exc.code})
                    continue
                raise
            if response == b"\0":
                # This can be a startup notification. Only successful RAM
                # transfer/execution below establishes an active updater.
                self.emit({"event": "force_candidate", "response_hex": response.hex(),
                           "attempt": attempts})
                return
            self.emit({"event": "force_response", "response_hex": response.hex(),
                       "attempt": attempts})
            if response != b"\x01":
                raise ValueError("ForceBoot rejected: " + response.hex())
        raise TimeoutError("No ForceBoot response; no firmware write was sent")

    def read_packet(self, deadline):
        while time.monotonic() < deadline:
            while self.buffer:
                start = self.buffer.find(b"\x7f")
                if start < 0:
                    self.buffer.clear()
                    break
                if start:
                    del self.buffer[:start]
                if len(self.buffer) < 5:
                    break
                try:
                    opcode, length = decode_header(self.buffer[:5])
                except ValueError:
                    del self.buffer[0]
                    continue
                size = 5 + length + bool(length)
                if len(self.buffer) < size:
                    break
                packet = bytes(self.buffer[:size])
                del self.buffer[:size]
                if length and sum(packet[5:-1]) & 255 != packet[-1]:
                    raise ValueError("UART response checksum mismatch")
                return opcode, packet[5:-1] if length else b""
            data = self.serial.read(self.serial.in_waiting or 1)
            if data:
                self.raw.write(data)
                self.raw.flush()
                self.buffer.extend(data)
        raise TimeoutError("UART response timed out")

    def exchange(self, opcode, payload, timeout):
        # This guard is independent of the update sequence and image loader.
        if not allowed_command(opcode, payload):
            raise ValueError("Erase, configuration, or unexpected command is forbidden")
        packet = b"".join(packet_parts(opcode, payload))
        self.emit(wire_command_event(opcode, payload, packet))
        written = self.serial.write(packet)
        self.serial.flush()
        if written != len(packet):
            raise OSError("Short UART write; no automatic retry")
        deadline = time.monotonic() + timeout
        while True:
            received, response = self.read_packet(deadline)
            if received == opcode:
                return response
            if received == 0xFF and len(response) == 2:
                self.emit({"event": "receiver_command_error", "failed_opcode": response[0],
                           "response_hex": response.hex()})
                raise ReceiverCommandError(response[0], int.from_bytes(response[1:], "little", signed=True))
            self.emit({"event": "unsolicited_packet", "opcode": received,
                       "response_hex": response.hex()})
            if received == 0xFF or received < 0x80:
                raise ValueError(f"Unexpected opcode 0x{received:02x}")


def run_update(transport, images, expected_version, emit, settle=time.sleep, reset_wait=5,
               resume_bootloader=False):
    validate_images(images)
    if resume_bootloader:
        emit({"event": "resume_bootloader", "current_version_read": False,
              "previously_expected_version": expected_version})
    else:
        before = transport.read_version()
        emit({"event": "version_before", "version": before})
        if before == TARGET_VERSION:
            return before
        if before != expected_version:
            raise ValueError("Initial firmware differs from the expected version")

    def checked(opcode, payload=b"", timeout=5, expected=b"\x00", **details):
        if not allowed_command(opcode, payload):
            raise ValueError("Forbidden command in update sequence")
        emit({"event": "command_sent", "opcode": opcode,
              "payload_bytes": len(payload),
              "operand_hex": payload.hex() if opcode != 1 else None, **details})
        response = transport.exchange(opcode, payload, timeout)
        emit({"event": "response", "opcode": opcode,
              "response_hex": response.hex(), **details})
        if response != expected:
            raise ValueError(f"Opcode 0x{opcode:02x} rejected: {response.hex()}")

    if not resume_bootloader:
        transport.stop()
        transport.force_boot(reset_wait)
    for name, data, kind in images:
        emit({"event": "image_started", "image": name, "bytes": len(data),
              "sha256": hashlib.sha256(data).hexdigest()})
        for payload in injection_frames(data):
            total, index = struct.unpack_from("<HH", payload)
            checked(0x01, payload, timeout=10, image=name, frame=index, total_frames=total)
            if kind is None and index == 1:
                emit({"event": "ram_transfer_confirmed"})
        if kind is None:
            checked(0x02, timeout=7, phase="execute_ram_updater")
            # A normal SDK still answering VER is evidence that ForceBoot did
            # not take effect. Stop before any nonvolatile write in that case.
            try:
                ram_version = transport.read_version()
            except TimeoutError as exc:
                emit({"event": "ram_version_probe", "normal_version_returned": False,
                      "detail": str(exc)})
            else:
                emit({"event": "ram_version_probe", "normal_version_returned": True,
                      "version": ram_version})
                raise RuntimeError("Normal firmware still responds; physical-reset ForceBoot is required. No SDK/APP/LIB write was sent")
        else:
            checked(0x09, bytes((kind, 0, 0)), timeout=30,
                    image=name, phase="write_image")
        emit({"event": "image_completed", "image": name})
    try:
        checked(0x00, b"\x01", expected=b"\x01", phase="boot")
    except TimeoutError:
        # UART can switch to the normal ASCII firmware without delivering the
        # binary boot acknowledgement. The target version below is mandatory.
        emit({"event": "boot_ack_missing", "version_verification_required": True})
    settle(1)
    after = transport.read_version()
    emit({"event": "version_after", "version": after})
    if after != TARGET_VERSION:
        raise ValueError(f"Expected {TARGET_VERSION}, received {after}")
    return after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--port", default="/dev/serial0")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--expected-version", default="v00.129")
    parser.add_argument("--wait-for-reset", type=float, default=5,
                        help="Seconds to repeat only ForceBoot requests while waiting for a physical reset")
    parser.add_argument("--resume-bootloader", action="store_true",
                        help="For a previously verified receiver already in the bootloader: skip normal query/ForceBoot, validate the RAM updater first")
    parser.add_argument("--log-dir", type=Path, required=True)
    args = parser.parse_args()
    if not 0 < args.wait_for_reset <= 3600:
        parser.error("--wait-for-reset must be between 0 and 3600 seconds")
    os.umask(0o077)
    images = load_package(args.package)[:4]
    validate_images(images)
    log_dir = create_log_dir(args.log_dir)
    summary = {"started_utc": datetime.now(timezone.utc).isoformat(),
               "argv": sys.argv, "mode": "write" if args.write else "offline_plan",
               "config_write_permitted": False, "erase_permitted": False,
               "resume_bootloader": args.resume_bootloader,
               "device_opened": False, "completed": False, "errors": [],
               "images": [{"name": n, "bytes": len(d), "kind": k,
                           "sha256": hashlib.sha256(d).hexdigest()} for n, d, k in images]}
    transport = None
    try:
        with (log_dir / "events.jsonl").open("x", buffering=1) as events, \
                (log_dir / "uart-rx.bin").open("xb") as raw:
            def emit(item):
                item = {"utc": datetime.now(timezone.utc).isoformat(), **item}
                events.write(json.dumps(item) + "\n")
                summary["last_event"] = item
                quiet = ("command_sent", "response", "wire_command_sent", "force_candidate",
                         "force_entry_not_ready", "receiver_command_error")
                ram_start = item["event"] == "image_started" and item.get("image") == "updater.efpk"
                if (item["event"] not in quiet and not ram_start) or item.get("phase"):
                    os.fsync(events.fileno())
                    print(json.dumps(item), flush=True)

            if args.write:
                transport = UARTTransport(args.port, args.baudrate, raw, emit)
                summary["device_opened"] = True
                summary["verified_version"] = run_update(
                    transport, images, args.expected_version, emit, reset_wait=args.wait_for_reset,
                    resume_bootloader=args.resume_bootloader)
            summary["completed"] = True
    except (Exception, KeyboardInterrupt) as exc:
        summary["errors"].append({"type": type(exc).__name__, "message": str(exc)})
        print(f"Stopped without erase, firmware retry or automatic reset: {exc}", file=sys.stderr, flush=True)
    finally:
        if transport is not None:
            transport.close()
        summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
        (log_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary), flush=True)
    return 0 if summary["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
