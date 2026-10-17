# Update command sequence adapted from Sony SIoT-Majipro.
# Copyright 2024 Sony Group Corporation
# SPDX-License-Identifier: Apache-2.0
"""Plan or perform the Sony Spresense GNSS Add-on v144 I2C update.

The command sequence follows Sony SIoT-Majipro's Apache-2.0 updater at
36d30533c9e419389b4367d2448bb6ce8aa2dfaa. Firmware is not distributed here.
Only the five audited images from the official v144 package are accepted.
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

from i2c_window_test import create_log_dir


TARGET_VERSION = "v00.144"
CHUNK_SIZE = 4086
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


def verify_image(name, data, size, digest):
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"Image size/hash mismatch: {name}")


def load_images(directory):
    images = []
    for name, size, digest, kind in IMAGES:
        path = directory / name
        if path.stat().st_size != size:
            raise ValueError(f"Unexpected image size: {name}")
        data = path.read_bytes()
        verify_image(name, data, size, digest)
        images.append((name, data, kind))
    return images


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


def injection_frames(data):
    count = (len(data) + CHUNK_SIZE - 1) // CHUNK_SIZE
    if not 1 <= count <= 65535:
        raise ValueError("Invalid firmware frame count")
    for index in range(count):
        yield struct.pack("<HH", count, index + 1) + data[index * CHUNK_SIZE:(index + 1) * CHUNK_SIZE]


def packet_parts(opcode, payload):
    if not 0 <= opcode <= 255 or len(payload) > 4090:
        raise ValueError("Invalid command size or opcode")
    header = struct.pack("<BHB", 0x7F, len(payload), opcode)
    return header + bytes((sum(header) & 255,)), payload + bytes((sum(payload) & 255,)) if payload else b""


def decode_header(header):
    if len(header) != 5 or header[0] != 0x7F or sum(header[:4]) & 255 != header[4]:
        raise ValueError("Invalid response header")
    length = int.from_bytes(header[1:3], "little")
    if length > 4090:
        raise ValueError("Response exceeds protocol limit")
    return header[3], length


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
        self.gpiod = gpiod
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
            if time.monotonic() >= deadline:
                raise TimeoutError("GNSS IRQ timed out")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("GNSS IRQ timed out")
            if self.request.wait_edge_events(timeout=min(remaining, 0.25)):
                self.request.read_edge_events()
                return

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
                self.ready(deadline)
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
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Command 0x{opcode:02x} timed out")
            received, response = self.read_packet(deadline)
            if received == opcode:
                return response
            if received >= 0x80 and received != 0xFF:
                continue
            if received == 0x00 and response in (b"\x01", b"\x02"):
                continue
            # The normal driver's close can leave its STOP acknowledgement queued.
            # Ignore it only during the initial read-only version query.
            if opcode == 0x06 and received == 0x31 and response == b"\x00":
                continue
            raise ValueError(f"Unexpected response opcode 0x{received:02x}")


def read_version(transport):
    response = transport.exchange(0x06, b"", 5)
    if not response or response[0] != 0:
        raise ValueError(f"Version query failed: {response.hex()}")
    version = response[1:].decode("ascii").strip("\x00\r\n ")
    if not version.startswith("v00."):
        raise ValueError(f"Unexpected firmware version: {version!r}")
    return version


def run_update(transport, images, expected_version, emit, settle=time.sleep, enter_mode="force"):
    if enter_mode not in ("force", "erase"):
        raise ValueError("Invalid update entry mode")
    before = read_version(transport)
    emit({"event": "version_before", "version": before})
    if before == TARGET_VERSION:
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

    checked(0x31)
    # Sony also documents erase-and-reboot for receivers rejecting force entry.
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
    if after != TARGET_VERSION:
        raise ValueError(f"Update finished, but expected {TARGET_VERSION}, received {after}")
    return after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", action="store_true", help="Perform the firmware update")
    action.add_argument("--query", action="store_true", help="Query the firmware version only")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--image-dir", type=Path)
    source.add_argument("--package", type=Path, help="Official v144 ZIP or SPK, obtained under Sony's EULA")
    parser.add_argument("--gpio-chip")
    parser.add_argument("--irq-line", type=int, default=17)
    parser.add_argument("--i2c-device", default="/dev/i2c-1")
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x24)
    parser.add_argument("--expected-version", default="v00.103")
    parser.add_argument("--enter-mode", choices=("force", "erase"), default="force",
                        help="Update entry: force (default) or erase eMRAM and reboot")
    parser.add_argument("--log-dir", type=Path)
    args = parser.parse_args()
    if not args.query and args.image_dir is None and args.package is None:
        parser.error("--package or --image-dir is required for planning or writing")
    if (args.write or args.query) and (sys.platform != "linux" or not args.gpio_chip):
        parser.error("Device access requires Linux and an explicitly verified --gpio-chip")
    if not 0 <= args.address <= 0x7F or args.irq_line < 0:
        parser.error("Invalid I2C address or IRQ line")
    os.umask(0o077)
    # Validate and retain every image in memory before opening any device.
    images = [] if args.query else load_package(args.package) if args.package else load_images(args.image_dir)
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    default = state / "rpi-cxd56xx-gnss" / datetime.now(timezone.utc).strftime("fwupdate-%Y%m%dT%H%M%S.%fZ")
    log_dir = create_log_dir(args.log_dir or default)
    summary = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "write" if args.write else "query" if args.query else "offline_plan",
        "target_version": TARGET_VERSION, "device_opened": False, "completed": False,
        "enter_mode": args.enter_mode,
        "images": [{"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "frames": (len(data) + CHUNK_SIZE - 1) // CHUNK_SIZE} for name, data, _ in images],
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
                transport = I2CTransport(args.i2c_device, args.address, args.gpio_chip, args.irq_line)
                summary["device_opened"] = True
                if args.write:
                    summary["verified_version"] = run_update(
                        transport, images, args.expected_version, emit, enter_mode=args.enter_mode)
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
