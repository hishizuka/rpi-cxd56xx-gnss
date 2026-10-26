# Update command sequence adapted from Sony SIoT-Majipro.
# Copyright 2024 Sony Group Corporation
# SPDX-License-Identifier: Apache-2.0
"""Plan or perform audited Sony Spresense GNSS Add-on I2C updates.

The command sequence follows Sony SIoT-Majipro's Apache-2.0 updater at
36d30533c9e419389b4367d2448bb6ce8aa2dfaa. Firmware is not distributed here.
The default profile accepts only the official v144 package. The experimental
v201 profile combines audited v201 code with the original Sony CFG.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import time
import zipfile

from _firmware_common import (
    CHUNK_SIZE, ReceiverCommandError, decode_header, image_summary, injection_frames,
    is_injection_frame, load_image_files, packet_parts, verify_image,
    wire_command_event,
)
from i2c_window_test import create_log_dir


TARGET_VERSION = "v00.144"
# Order, sizes and hashes belong to the official v144 Spresense package.
IMAGES = (
    ("updater.efpk", 30592, "f4c335d2820d6e174e0eb9bb3828a73527f558b09e2272d7f52456a18c0b3b37", None),
    ("cxd5610_sdk.efpk", 719040, "6f8a031b04b82fc4d1f4f74ab343f50db1d338eeb40b20b6a095ba9977dfef04", 0),
    ("cxd5610_app.efpk", 258032, "da85349e043a744462a27ebcfef19537d0eb4d9088b8e18315ed88a7f27b4def", 1),
    ("cxd5610_lib.efpk", 1088, "cedaebe58fa8c702caae5cb6fb42449d1db20adbea6bafdbf30a82589144fd5c", 2),
    ("spresense_i2c.cfg", 3008, "f903fe844e52d9d014eb86a4d72150d6b53ca9cae8857f6789562838b7eb7b0c", 3),
)
SPK_NAME = "gnss_addon_fw_updater_v144.spk"
SPK_SIZE = 1177488
SPK_SHA256 = "37d2294b8edd21ff24596333e850cedc7c7cc8e9de73c42129f93336e7ed2b2c"
ZIP_SIZE = 1129812
ZIP_SHA256 = "86609ae9e0041df6251034fa48b90401708cd63e1e457c4f2063b59e697901dd"
# Offsets were audited against the official SPK pointer tables and size fields.
# They must only be used after verifying the complete SPK hash.
SPK_IMAGE_OFFSETS = (1146068, 424012, 164881, 422920, 1143056)


def load_images(directory):
    return load_image_files(directory, IMAGES)


def load_package(path):
    # Never execute an SPK or extract archive paths onto the filesystem.
    size = path.stat().st_size
    if size not in (SPK_SIZE, ZIP_SIZE):
        raise ValueError("Unexpected v144 package size")
    with path.open("rb") as source:
        data = source.read(max(SPK_SIZE, ZIP_SIZE) + 1)
    if size == ZIP_SIZE:
        if len(data) != ZIP_SIZE or hashlib.sha256(data).hexdigest() != ZIP_SHA256:
            raise ValueError("Official v144 ZIP hash mismatch")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            member = archive.getinfo(SPK_NAME)
            if member.file_size != SPK_SIZE:
                raise ValueError("Unexpected embedded SPK size")
            data = archive.read(member)
    if len(data) != SPK_SIZE or hashlib.sha256(data).hexdigest() != SPK_SHA256:
        raise ValueError("Official v144 SPK hash mismatch")
    images = []
    for (name, size, digest, kind), offset in zip(IMAGES, SPK_IMAGE_OFFSETS):
        image = data[offset:offset + size]
        verify_image(name, image, size, digest)
        images.append((name, image, kind))
    return images


def load_spresense_v201_images(code_directory, sony_package):
    # Keep the code directory CFG-free; obtain CFG only from the verified Sony SPK.
    from i2c_firmware_update_preserve import load_images as load_code_images

    code = load_code_images(code_directory, "v201")
    cfg = load_package(sony_package)[-1]
    name, size, digest, kind = IMAGES[-1]
    if cfg[0] != name or cfg[2] != kind:
        raise ValueError("Original Spresense CFG is required")
    verify_image(name, cfg[1], size, digest)
    return code + [cfg]


class I2CTransport:
    def __init__(self, device, address, chip, irq):
        import fcntl
        import gpiod
        from gpiod.line import Direction, Edge, Value

        self.fd = None
        self.request = None
        self.irq = irq
        self.initial_wait = True
        self.active = Value.ACTIVE
        try:
            # Exclusive ownership of IRQ also prevents another normal driver.
            self.request = gpiod.request_lines(
                chip, consumer="cxd5610_fwupdate",
                config={irq: gpiod.LineSettings(direction=Direction.INPUT, edge_detection=Edge.RISING)},
            )
            self.fd = os.open(device, os.O_RDWR)
            fcntl.ioctl(self.fd, 0x0703, address)  # I2C_SLAVE, without FORCE.
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.request is not None:
            self.request.release()
            self.request = None

    def discard_edges(self):
        while self.request.wait_edge_events(timeout=0):
            self.request.read_edge_events()

    def ready(self, deadline):
        # Only the first transfer may use an IRQ asserted before line ownership.
        # Later transfers need a fresh edge: the old level can remain high briefly
        # after a read, before the receiver has prepared the next packet part.
        if self.initial_wait:
            self.initial_wait = False
            self.discard_edges()
            if self.request.get_value(self.irq) == self.active:
                return
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("GNSS IRQ timed out")
            if self.request.wait_edge_events(timeout=min(remaining, 0.25)):
                self.request.read_edge_events()
                return

    def ready_body(self, deadline):
        self.ready(deadline)

    def read_packet(self, deadline):
        self.ready(deadline)
        self.discard_edges()
        try:
            header = os.read(self.fd, 5)
        except OSError as exc:
            raise OSError(exc.errno, f"I2C response header read failed: {exc}") from exc
        opcode, length = decode_header(header)
        body = bytearray()
        if length:
            remaining = length + 1
            while remaining:
                self.ready_body(deadline)
                self.discard_edges()
                size = min(512, remaining)
                part = os.read(self.fd, size)
                if len(part) != size:
                    raise OSError("Short I2C response")
                body.extend(part)
                remaining -= size
            if sum(body[:-1]) & 255 != body[-1]:
                raise ValueError("Response checksum mismatch")
        return opcode, bytes(body[:-1]) if length else b""

    def exchange(self, opcode, payload, timeout):
        # Match the working Pi driver: send each complete packet in one write.
        # Separate header/operand writes held SCL low on the v00.103 board.
        packet = b"".join(packet_parts(opcode, payload))
        try:
            written = os.write(self.fd, packet)
        except OSError as exc:
            raise OSError(exc.errno, f"Command 0x{opcode:02x} I2C write failed: {exc}") from exc
        if written != len(packet):
            raise OSError("Short I2C command; transfer state is unknown")
        return self._read_response(opcode, time.monotonic() + timeout)

    def _read_response(self, opcode, deadline, emit=None):
        while time.monotonic() < deadline:
            received, response = self.read_packet(deadline)
            if received == opcode:
                return response
            if received == 0xFF and len(response) == 2:
                if emit is not None:
                    emit({"event": "receiver_command_error", "response_hex": response.hex()})
                raise ReceiverCommandError(response[0], int.from_bytes(response[1:], "little", signed=True))
            if received >= 0x80 and received != 0xFF:
                continue
            if received == 0x00 and response in (b"\x01", b"\x02"):
                continue
            # The normal driver's close can leave its STOP acknowledgement queued.
            # Ignore it only during the initial read-only version query.
            if opcode == 0x06 and received == 0x31 and response == b"\x00":
                continue
            raise ValueError(f"Unexpected response opcode 0x{received:02x}")
        raise TimeoutError(f"Command 0x{opcode:02x} timed out")


def allowed_spresense_reflash_command(opcode, payload):
    if not payload and opcode in (0x02, 0x06, 0x31):
        return True
    if opcode == 0:
        return payload in (b"\x00", b"\x01")
    if opcode == 9:
        return payload in (b"\0\0\0", b"\x01\0\0", b"\x02\0\0", b"\x03\x01\0")
    if opcode == 1:
        return is_injection_frame(payload)
    return False


class SpresenseReflashTransport(I2CTransport):
    def __init__(self, device, address, chip, irq, emit):
        self.emit = emit
        self.bootloader_protocol = False
        super().__init__(device, address, chip, irq)

    def ready_body(self, deadline):
        if not self.bootloader_protocol:
            return super().ready_body(deadline)
        # Sony's updater waits 100 ms, then reads the declared body even without
        # another edge. The ROM can keep IRQ high between header and body.
        # Require HIGH and retain the normal driver's fresh-edge rule elsewhere.
        try:
            self.ready(min(deadline, time.monotonic() + 0.1))
        except TimeoutError:
            if time.monotonic() >= deadline or self.request.get_value(self.irq) != self.active:
                raise
            self.emit({"event": "bootloader_body_level_ready"})

    def read_packet(self, deadline):
        opcode, payload = super().read_packet(deadline)
        if opcode < 0x80 or opcode == 0xFF:
            self.emit({"event": "wire_response", "opcode": opcode, "response_hex": payload.hex()})
        return opcode, payload

    def exchange(self, opcode, payload, timeout):
        # This explicitly destructive policy is separate from the preserving tool.
        if not allowed_spresense_reflash_command(opcode, payload):
            raise ValueError("Command is outside the audited Spresense reflash policy")
        packet = b"".join(packet_parts(opcode, payload))
        self.emit(wire_command_event(opcode, payload, packet))
        if (opcode, payload) == (0, b"\0"):
            self.bootloader_protocol = True
        response = super().exchange(opcode, payload, timeout)
        if (opcode, payload, response) == (0, b"\x01", b"\x01"):
            self.bootloader_protocol = False
        return response


def validate_erase_resume(directory, images, target_version, profile="v201"):
    targets = {"v144": TARGET_VERSION, "v201": "v00.201"}
    if targets.get(profile) != target_version:
        raise ValueError("Resume profile and target version do not match")
    summary = json.loads((directory / "summary.json").read_text())
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    expected = image_summary(images)
    wire = [event for event in events if event["event"] == "wire_command_sent"]
    if (summary.get("completed") or summary.get("profile") != profile
            or summary.get("target_version") != target_version or summary.get("images") != expected
            or summary.get("enter_mode") != "erase" or not wire
            or (wire[-1].get("opcode"), wire[-1].get("operand_hex")) != (0, "00")
            or any(event["event"] == "image_started" for event in events)
            or not any(error.get("type") == "TimeoutError" for error in summary.get("errors", []))):
        raise ValueError("Resume requires matching images and an erase-entry timeout before any image transfer")


def read_version(transport):
    response = transport.exchange(0x06, b"", 5)
    if not response or response[0] != 0:
        raise ValueError(f"Version query failed: {response.hex()}")
    version = response[1:].decode("ascii").strip("\x00\r\n ")
    if not version.startswith("v00."):
        raise ValueError(f"Unexpected firmware version: {version!r}")
    return version


def run_update(transport, images, expected_version, emit, settle=time.sleep,
               enter_mode="force", target_version=TARGET_VERSION, resume_after_erase=False):
    if enter_mode not in ("force", "erase"):
        raise ValueError("Invalid update entry mode")
    if resume_after_erase:
        if enter_mode != "erase":
            raise ValueError("Resume requires explicit erase mode")
        transport.bootloader_protocol = True
        try:
            version = read_version(transport)
        except ReceiverCommandError as exc:
            if (exc.opcode, exc.code) != (6, -2):
                raise
            emit({"event": "bootloader_resume_verified", "probe_opcode": 6, "error": -2})
        else:
            raise ValueError(f"Normal firmware {version} still responds; no resume transfer started")
    else:
        before = read_version(transport)
        emit({"event": "version_before", "version": before})
        if before == target_version:
            emit({"event": "already_updated", "version": before})
            return before
        if before != expected_version:
            raise ValueError(f"Expected {expected_version}, received {before}; no update started")

    def checked(opcode, payload=b"", timeout=5, expected=b"\x00", **details):
        emit({"event": "command_sent", "opcode": opcode, "payload_bytes": len(payload), **details})
        response = transport.exchange(opcode, payload, timeout)
        emit({"event": "response", "opcode": opcode, "response_hex": response.hex(), **details})
        if response != expected:
            raise ValueError(f"Command 0x{opcode:02x} failed: {response.hex()}")

    if not resume_after_erase:
        checked(0x31)
        # Never fall back automatically: erasing is an explicit invocation choice.
        checked(0x00, b"\x00" if enter_mode == "erase" else b"\xff",
                phase="enter_update", enter_mode=enter_mode)
    for name, data, kind in images:
        emit({"event": "image_started", "image": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        for payload in injection_frames(data):
            total, serial = struct.unpack_from("<HH", payload)
            checked(0x01, payload, timeout=10, image=name, frame=serial, total_frames=total)
        if kind is None:
            checked(0x02, timeout=7, phase="execute_updater")
        else:
            # EFPK is file type 0; the board configuration is plain FPK type 1.
            checked(0x09, bytes((kind, int(name.endswith(".cfg")), 0)), timeout=30, image=name, phase="write_image")
        emit({"event": "image_completed", "image": name})
    checked(0x00, b"\x01", expected=b"\x01", phase="reset")
    settle(1)
    after = read_version(transport)
    emit({"event": "version_after", "version": after})
    if after != target_version:
        raise ValueError(f"Update finished, but expected {target_version}, received {after}")
    return after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("v144", "v201"), default="v144")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", action="store_true", help="Perform the firmware update")
    action.add_argument("--query", action="store_true", help="Query the firmware version only")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--image-dir", type=Path)
    source.add_argument("--package", type=Path, help="Official v144 ZIP or SPK, obtained under Sony's EULA")
    parser.add_argument("--code-dir", type=Path, help="Audited v201 code-only directory; requires Sony --package")
    parser.add_argument("--gpio-chip")
    parser.add_argument("--irq-line", type=int, default=17)
    parser.add_argument("--i2c-device", default="/dev/i2c-1")
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x24)
    parser.add_argument("--expected-version", help="Installed version; default v00.103 for v144, v00.144 for v201")
    parser.add_argument("--enter-mode", choices=("force", "erase"), default="force",
                        help="Update entry: force (default) or erase eMRAM and reboot")
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--resume-after-erase", type=Path,
                        help="Prior matching erase-entry timeout log; resume without another erase")
    args = parser.parse_args()
    v201 = args.profile == "v201"
    target_version = "v00.201" if v201 else TARGET_VERSION
    expected_version = args.expected_version or ("v00.144" if v201 else "v00.103")
    if v201 and not args.query and (args.package is None or args.code_dir is None):
        parser.error("v201 requires --package and --code-dir")
    if v201 and args.write and args.enter_mode != "erase":
        parser.error("v201 writes require explicit --enter-mode erase and restore the original Sony CFG")
    if args.resume_after_erase and not (args.write and args.enter_mode == "erase"):
        parser.error("--resume-after-erase requires --write --enter-mode erase")
    if not v201 and args.code_dir is not None:
        parser.error("--code-dir is only for v201")
    if not args.query and args.image_dir is None and args.package is None:
        parser.error("--package or --image-dir is required for planning or writing")
    if (args.write or args.query) and (sys.platform != "linux" or not args.gpio_chip):
        parser.error("Device access requires Linux and an explicitly verified --gpio-chip")
    if not 0 <= args.address <= 0x7F or args.irq_line < 0:
        parser.error("Invalid I2C address or IRQ line")
    os.umask(0o077)
    # Validate and retain every image in memory before opening any device.
    images = [] if args.query else (
        load_spresense_v201_images(args.code_dir, args.package) if v201 else
        load_package(args.package) if args.package else load_images(args.image_dir))
    if args.resume_after_erase:
        validate_erase_resume(args.resume_after_erase, images, target_version, args.profile)
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    default = state / "rpi-cxd56xx-gnss" / datetime.now(timezone.utc).strftime("fwupdate-%Y%m%dT%H%M%S.%fZ")
    log_dir = create_log_dir(args.log_dir or default)
    summary = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "write" if args.write else "query" if args.query else "offline_plan",
        "profile": args.profile, "target_version": target_version, "device_opened": False, "completed": False,
        "enter_mode": args.enter_mode,
        "resume_after_erase": str(args.resume_after_erase) if args.resume_after_erase else None,
        "config_source": "official Sony v144 package" if v201 else None,
        "images": image_summary(images),
        "planned_program_operands": [bytes((kind, int(name.endswith('.cfg')), 0)).hex() for name, _, kind in images if kind is not None],
        "argv": sys.argv, "errors": [],
    }
    print(f"Logs: {log_dir}", flush=True)
    transport = None
    try:
        with (log_dir / "events.jsonl").open("x", buffering=1) as output:
            def emit(event):
                event = {"utc": datetime.now(timezone.utc).isoformat(), **event}
                output.write(json.dumps(event) + "\n")
                if event["event"] in ("image_started", "image_completed", "version_before", "version_after", "already_updated"):
                    os.fsync(output.fileno())
                    print(json.dumps(event), flush=True)
                elif event.get("phase") == "write_image" and event["event"] == "command_sent":
                    os.fsync(output.fileno())
                summary["last_event"] = event

            if args.write or args.query:
                transport = (SpresenseReflashTransport(args.i2c_device, args.address, args.gpio_chip, args.irq_line, emit)
                             if args.write and args.enter_mode == "erase" else
                             I2CTransport(args.i2c_device, args.address, args.gpio_chip, args.irq_line))
                summary["device_opened"] = True
                if args.write:
                    summary["verified_version"] = run_update(
                        transport, images, expected_version, emit,
                        enter_mode=args.enter_mode, target_version=target_version,
                        resume_after_erase=bool(args.resume_after_erase))
                else:
                    summary["verified_version"] = read_version(transport)
                    emit({"event": "query", "version": summary["verified_version"]})
            summary["completed"] = True
    except (Exception, KeyboardInterrupt) as exc:
        summary["errors"].append({"type": type(exc).__name__, "message": str(exc)})
        # Preserve an in-RAM updater after failure; never erase, reset, or retry.
        print(f"Stopped without automatic retry/reset: {exc}", file=sys.stderr, flush=True)
    finally:
        if transport is not None:
            transport.close()
        summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
        (log_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary), flush=True)
    return 0 if summary["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
