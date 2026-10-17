# SPDX-License-Identifier: Apache-2.0
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import i2c_firmware_update as fw


class SimulatedReceiver:
    def __init__(self, images, version="v00.103", fail_slot=None, timeout_frame=None):
        self.images = images
        self.version = version
        self.fail_slot = fail_slot
        self.timeout_frame = timeout_frame
        self.index = 0
        self.received = bytearray()
        self.serial = 0
        self.calls = []
        self.entered = False
        self.verified_images = []

    def exchange(self, opcode, payload, timeout):
        self.calls.append((opcode, payload))
        if opcode == 0x06:
            return b"\0" + self.version.encode() + b"\r\n"
        if opcode == 0x31:
            return b"\0"
        if opcode == 0x00 and payload in (b"\xff", b"\x00"):
            self.entered = True
            return b"\0"
        if opcode == 0x00 and payload == b"\x01":
            if self.index != len(self.images):
                raise AssertionError("Reset before all images were committed")
            self.version = fw.TARGET_VERSION
            return b"\x01"
        if not self.entered:
            raise AssertionError("Transfer before update mode")
        name, expected_data, kind = self.images[self.index]
        if opcode == 0x01:
            total, serial = struct.unpack_from("<HH", payload)
            if self.timeout_frame == serial:
                raise TimeoutError("Simulated missing acknowledgement")
            if serial != self.serial + 1 or total != (len(expected_data) + 4085) // 4086:
                raise AssertionError("Invalid frame sequence")
            self.serial = serial
            self.received.extend(payload[4:])
            return b"\0"
        if bytes(self.received) != expected_data:
            raise AssertionError("Transferred image differs from its input")
        if kind is None:
            if opcode != 0x02 or payload:
                raise AssertionError("Updater must execute in RAM")
        else:
            if opcode != 0x09 or payload != bytes((kind, int(name.endswith('.cfg')), 0)):
                raise AssertionError("Wrong image slot or encoding")
            if kind == self.fail_slot:
                return b"\xea"
        self.verified_images.append(name)
        self.index += 1
        self.serial = 0
        self.received.clear()
        return b"\0"


class FirmwareUpdateTests(unittest.TestCase):
    def images(self):
        return [
            ("updater.efpk", bytes(range(256)) * 16, None),
            ("cxd5610_sdk.efpk", b"s" * 8173, 0),
            ("cxd5610_app.efpk", b"a" * 4086, 1),
            ("cxd5610_lib.efpk", b"l", 2),
            ("spresense_i2c.cfg", b"config", 3),
        ]

    def test_full_sequence_preserves_all_images_and_verifies_version(self):
        images = self.images()
        receiver = SimulatedReceiver(images)
        events = []
        result = fw.run_update(receiver, images, "v00.103", events.append, settle=lambda _: None)
        self.assertEqual(result, "v00.144")
        self.assertEqual(receiver.verified_images, [image[0] for image in images])
        self.assertEqual(receiver.calls[-2:], [(0, b"\x01"), (6, b"")])

    def test_negative_write_response_prevents_later_writes_and_reset(self):
        receiver = SimulatedReceiver(self.images(), fail_slot=1)
        with self.assertRaisesRegex(ValueError, "failed: ea"):
            fw.run_update(receiver, self.images(), "v00.103", lambda _: None)
        self.assertEqual(receiver.calls[-1], (9, b"\x01\0\0"))
        self.assertNotIn((0, b"\x01"), receiver.calls)
        self.assertNotIn("cxd5610_lib.efpk", receiver.verified_images)

    def test_erase_entry_is_explicit_and_preserves_the_transfer_sequence(self):
        receiver = SimulatedReceiver(self.images())
        result = fw.run_update(receiver, self.images(), "v00.103", lambda _: None,
                               settle=lambda _: None, enter_mode="erase")
        self.assertEqual(result, "v00.144")
        self.assertEqual(receiver.calls[2], (0, b"\x00"))
        self.assertNotIn((0, b"\xff"), receiver.calls)

    def test_force_entry_rejection_does_not_fall_back_to_erasing(self):
        receiver = SimulatedReceiver(self.images())
        exchange = receiver.exchange
        receiver.exchange = lambda opcode, payload, timeout: (
            b"\xea" if (opcode, payload) == (0, b"\xff") else exchange(opcode, payload, timeout))
        with self.assertRaisesRegex(ValueError, "failed: ea"):
            fw.run_update(receiver, self.images(), "v00.103", lambda _: None)
        self.assertFalse(receiver.entered)
        self.assertNotIn((0, b"\x00"), receiver.calls)

    def test_timeout_does_not_repeat_a_firmware_frame(self):
        receiver = SimulatedReceiver(self.images(), timeout_frame=2)
        with self.assertRaises(TimeoutError):
            fw.run_update(receiver, self.images(), "v00.103", lambda _: None)
        frames = [struct.unpack_from("<HH", data)[1] for opc, data in receiver.calls if opc == 1]
        self.assertEqual(frames, [1, 2])
        self.assertEqual(receiver.index, 0)

    def test_wrong_initial_version_prevents_all_mutations(self):
        receiver = SimulatedReceiver(self.images(), version="v00.999")
        with self.assertRaisesRegex(ValueError, "no update started"):
            fw.run_update(receiver, self.images(), "v00.103", lambda _: None)
        self.assertEqual(receiver.calls, [(6, b"")])

    def test_already_updated_does_not_reflash(self):
        receiver = SimulatedReceiver(self.images(), version="v00.144")
        self.assertEqual(fw.run_update(receiver, self.images(), "v00.103", lambda _: None), "v00.144")
        self.assertEqual(receiver.calls, [(6, b"")])

    def test_corrupt_image_is_rejected_before_device_access(self):
        data = b"verified firmware"
        specs = (("updater.efpk", len(data), hashlib.sha256(data).hexdigest(), None),)
        with tempfile.TemporaryDirectory() as tmp, patch.object(fw, "IMAGES", specs):
            path = Path(tmp) / "updater.efpk"
            path.write_bytes(data)
            self.assertEqual(fw.load_images(Path(tmp))[0][1], data)
            path.write_bytes(b"x" + data[1:])
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                fw.load_images(Path(tmp))

    def test_wire_frame_is_bounded_and_has_expected_encoding(self):
        payload = next(fw.injection_frames(b"\xff" * 4086))
        header, body = fw.packet_parts(1, payload)
        self.assertEqual(header, bytes.fromhex("7ffa0f0189"))
        self.assertEqual(body[:4], bytes.fromhex("01000100"))
        self.assertEqual(len(body), 4091)
        self.assertEqual(len(header + body), 4096)
        self.assertEqual(body[-1], sum(body[:-1]) % 256)
        self.assertEqual(fw.packet_parts(2, b""), (bytes.fromhex("7f00000281"), b""))
        with self.assertRaises(ValueError):
            fw.packet_parts(1, b"x" * 4091)

    def test_package_hash_is_verified_before_using_embedded_offsets(self):
        data = b"SPKprefixIMAGEsuffix"
        image = b"IMAGE"
        specs = (("updater.efpk", len(image), hashlib.sha256(image).hexdigest(), None),)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fw, "SPK_SIZE", len(data)), \
                patch.object(fw, "SPK_SHA256", hashlib.sha256(data).hexdigest()), \
                patch.object(fw, "IMAGES", specs), \
                patch.object(fw, "SPK_IMAGE_OFFSETS", (9,)):
            path = Path(tmp) / "firmware.spk"
            path.write_bytes(data)
            self.assertEqual(fw.load_package(path), [("updater.efpk", image, None)])
            path.write_bytes(b"x" + data[1:])
            with self.assertRaisesRegex(ValueError, "SPK hash mismatch"):
                fw.load_package(path)

    def test_transport_sends_header_and_operand_in_one_transaction(self):
        transport = object.__new__(fw.I2CTransport)
        transport.fd = 123
        transport.read_packet = lambda deadline: (0, b"\0")
        with patch.object(fw.os, 'write', side_effect=lambda fd, data: len(data)) as write:
            self.assertEqual(transport.exchange(0, b"\xff", 5), b"\0")
            write.assert_called_once_with(123, bytes.fromhex('7f01000080ffff'))

    def test_short_write_is_not_retried(self):
        transport = object.__new__(fw.I2CTransport)
        transport.fd = 123
        with patch.object(fw.os, 'write', return_value=5) as write:
            with self.assertRaisesRegex(OSError, 'Short I2C command'):
                transport.exchange(0, b"\xff", 5)
            self.assertEqual(write.call_count, 1)

    def test_version_query_skips_a_queued_normal_driver_stop_acknowledgement(self):
        transport = object.__new__(fw.I2CTransport)
        transport.fd = 123
        responses = iter([(0x31, b"\0"), (0x06, b"\0v00.144\r\n")])
        transport.read_packet = lambda deadline: next(responses)
        with patch.object(fw.os, "write", side_effect=lambda fd, data: len(data)) as write:
            self.assertEqual(fw.read_version(transport), "v00.144")
            self.assertEqual(write.call_count, 1)

    def test_old_high_irq_cannot_read_the_response_body_prematurely(self):
        class Request:
            body_ready = False

            def wait_edge_events(self, timeout):
                if timeout > 0:
                    self.body_ready = True
                    return True
                return False

            def read_edge_events(self):
                return []

            def get_value(self, irq):
                return 1

        request = Request()
        transport = object.__new__(fw.I2CTransport)
        transport.fd = 123
        transport.request = request
        transport.irq = 17
        transport.active = 1
        transport.initial_wait = True
        header = bytes.fromhex("7f02000687")

        def read(fd, size):
            if size == 5:
                return header
            self.assertTrue(request.body_ready, "The previous high level is not a new body-ready IRQ")
            self.assertEqual(size, 3)
            return b"\x00v\x76"

        with patch.object(fw.os, "read", side_effect=read):
            self.assertEqual(transport.read_packet(time.monotonic() + 1), (6, b"\x00v"))

    def test_corrupt_response_header_is_rejected(self):
        self.assertEqual(fw.decode_header(bytes.fromhex("7f01000181")), (1, 1))
        with self.assertRaises(ValueError):
            fw.decode_header(bytes.fromhex("7f01000180"))


if __name__ == "__main__":
    unittest.main()
