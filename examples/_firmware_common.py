# SPDX-License-Identifier: Apache-2.0
"""Shared firmware image validation and CXD5610 binary packet helpers."""

import hashlib
import struct


CHUNK_SIZE = 4086


class ReceiverCommandError(ValueError):
    def __init__(self, opcode, code):
        self.opcode, self.code = opcode, code
        super().__init__(f"Receiver rejected opcode 0x{opcode:02x}: error {code}")


def verify_image(name, data, size, digest):
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"Image size/hash mismatch: {name}")


def load_image_files(directory, specs):
    images = []
    for name, size, digest, kind in specs:
        path = directory / name
        if path.stat().st_size != size:
            raise ValueError(f"Unexpected image size: {name}")
        with path.open("rb") as source:
            data = source.read(size + 1)
        verify_image(name, data, size, digest)
        images.append((name, data, kind))
    return images


def validate_code_images(images, specs):
    if len(images) != 4:
        raise ValueError("Exactly RAM updater, SDK, APP and LIB are required; CFG is forbidden")
    for (name, data, kind), (expected, size, digest, expected_kind) in zip(images, specs):
        if name != expected or kind != expected_kind:
            raise ValueError("Unexpected image name/order/slot; CFG and mixed profiles are forbidden")
        verify_image(name, data, size, digest)


def injection_frames(data):
    count = (len(data) + CHUNK_SIZE - 1) // CHUNK_SIZE
    if not 1 <= count <= 65535:
        raise ValueError("Invalid firmware frame count")
    for index in range(count):
        yield struct.pack("<HH", count, index + 1) + data[index * CHUNK_SIZE:(index + 1) * CHUNK_SIZE]


def is_injection_frame(payload):
    if not 5 <= len(payload) <= 4090:
        return False
    total, serial = struct.unpack_from("<HH", payload)
    return 1 <= serial <= total <= 65535


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


def wire_command_event(opcode, payload, packet):
    return {
        "event": "wire_command_sent", "opcode": opcode,
        "operand_hex": payload.hex() if opcode != 1 else None,
        "packet_bytes": len(packet), "packet_sha256": hashlib.sha256(packet).hexdigest(),
    }


def image_summary(images):
    return [
        {"name": name, "kind": kind, "bytes": len(data),
         "sha256": hashlib.sha256(data).hexdigest(),
         "frames": (len(data) + CHUNK_SIZE - 1) // CHUNK_SIZE}
        for name, data, kind in images
    ]
