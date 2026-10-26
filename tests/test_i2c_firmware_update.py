# SPDX-License-Identifier: Apache-2.0
import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import i2c_firmware_update as fw
import i2c_firmware_update_preserve as preserve


class SimulatedReceiver:
    def __init__(self, images, version="v00.103", fail_slot=None, timeout_frame=None,
                 target_version=fw.TARGET_VERSION):
        self.images = images
        self.version = version
        self.target_version = target_version
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
            self.version = self.target_version
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
    def images(self, updater="updater.efpk"):
        return [
            (updater, bytes(range(256)) * 16, None),
            ("cxd5610_sdk.efpk", b"s" * 8173, 0),
            ("cxd5610_app.efpk", b"a" * 4086, 1),
            ("cxd5610_lib.efpk", b"l", 2),
            ("spresense_i2c.cfg", b"config", 3),
        ]

    def test_full_sequence_preserves_all_images_and_verifies_version(self):
        cases = (
            ("v00.103", "v00.144", "updater.efpk", "force"),
            ("v00.201", "v00.144", "updater.efpk", "erase"),
            ("v00.144", "v00.201", "cxd5610_updater.efpk", "erase"),
        )
        for before, target, updater, entry in cases:
            with self.subTest(before=before, target=target, entry=entry):
                images = self.images(updater)
                receiver = SimulatedReceiver(images, version=before, target_version=target)
                result = fw.run_update(receiver, images, before, lambda _: None,
                                       settle=lambda _: None, enter_mode=entry, target_version=target)
                self.assertEqual(result, target)
                self.assertEqual(receiver.verified_images, [image[0] for image in images])
                self.assertEqual(receiver.calls[-2:], [(0, b"\x01"), (6, b"")])
                operand = b"\0" if entry == "erase" else b"\xff"
                self.assertEqual(receiver.calls[2], (0, operand))
                self.assertEqual(receiver.calls.count((0, operand)), 1)
                self.assertNotIn((0, b"\xff" if entry == "erase" else b"\0"), receiver.calls)
                self.assertEqual([payload for opcode, payload in receiver.calls if opcode == 9],
                                 [b"\0\0\0", b"\x01\0\0", b"\x02\0\0", b"\x03\x01\0"])

    def test_negative_write_response_prevents_later_writes_and_reset(self):
        receiver = SimulatedReceiver(self.images(), fail_slot=1)
        with self.assertRaisesRegex(ValueError, "failed: ea"):
            fw.run_update(receiver, self.images(), "v00.103", lambda _: None)
        self.assertEqual(receiver.calls[-1], (9, b"\x01\0\0"))
        self.assertNotIn((0, b"\x01"), receiver.calls)
        self.assertNotIn("cxd5610_lib.efpk", receiver.verified_images)

    def test_v144_erase_cli_uses_rom_body_reading_and_audited_command_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            receiver = SimulatedReceiver(self.images(), version="v00.201")
            receiver.close = lambda: None
            argv = ["update", "--package", "sony.zip", "--write", "--enter-mode", "erase",
                    "--expected-version", "v00.201", "--gpio-chip", "/dev/gpiochip0",
                    "--log-dir", str(Path(tmp)/"write")]
            with patch.object(sys, "argv", argv), patch.object(fw.sys, "platform", "linux"), \
                    patch.object(fw, "load_package", return_value=self.images()), \
                    patch.object(fw, "SpresenseReflashTransport", return_value=receiver) as rom, \
                    patch.object(fw, "I2CTransport") as normal, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fw.main(), 0)
            rom.assert_called_once()
            normal.assert_not_called()
            summary = json.loads((Path(tmp)/"write/summary.json").read_text())
            self.assertEqual(summary["verified_version"], "v00.144")
            self.assertTrue(summary["completed"])

    def test_resume_requires_matching_profile_images_and_no_prior_transfer(self):
        profiles = (
            ("v144", "v00.144", "updater.efpk"),
            ("v201", "v00.201", "cxd5610_updater.efpk"),
        )
        for profile, target, updater in profiles:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                images = self.images(updater)
                summary = {"profile": profile, "target_version": target, "completed": False,
                           "enter_mode": "erase", "images": fw.image_summary(images),
                           "errors": [{"type": "TimeoutError"}]}
                events = [{"event": "wire_command_sent", "opcode": 0, "operand_hex": "00"}]
                root = Path(tmp)
                (root/"summary.json").write_text(json.dumps(summary))
                (root/"events.jsonl").write_text(json.dumps(events[0])+"\n")
                fw.validate_erase_resume(root, images, target, profile)
                other_profile = next(name for name, _, _ in profiles if name != profile)
                with self.assertRaises(ValueError):
                    fw.validate_erase_resume(root, images, target, other_profile)
                with self.assertRaises(ValueError):
                    fw.validate_erase_resume(root, images, "v00.999", profile)
                (root/"events.jsonl").write_text("\n".join(json.dumps(event) for event in
                    [*events, {"event": "image_started", "image": images[0][0]}])+"\n")
                with self.assertRaises(ValueError):
                    fw.validate_erase_resume(root, images, target, profile)
                (root/"events.jsonl").write_text(json.dumps(events[0])+"\n")
                summary["images"][-1]["sha256"] = "changed"
                (root/"summary.json").write_text(json.dumps(summary))
                with self.assertRaises(ValueError):
                    fw.validate_erase_resume(root, images, target, profile)

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
        for target in ("v00.144", "v00.201"):
            with self.subTest(target=target):
                receiver = SimulatedReceiver(self.images(), version=target)
                self.assertEqual(fw.run_update(receiver, self.images(), "v00.103", lambda _: None,
                                               target_version=target), target)
                self.assertEqual(receiver.calls, [(6, b"")])

    def test_old_version_after_programming_is_not_success(self):
        for before, target in (("v00.103", "v00.144"), ("v00.144", "v00.201")):
            with self.subTest(before=before, target=target):
                receiver = SimulatedReceiver(self.images(), version=before, target_version=before)
                with self.assertRaisesRegex(ValueError, f"expected {target}"):
                    fw.run_update(receiver, self.images(), before, lambda _: None,
                                  settle=lambda _: None, enter_mode="erase", target_version=target)

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
        for kind in (fw.I2CTransport, preserve.PreservingI2CTransport):
            with self.subTest(transport=kind.__name__):
                transport = object.__new__(kind)
                transport.fd = 123
                transport.emit = lambda _: None
                responses = iter([
                    (0x8B, b"notification"), (0, b"\x01"), (0, b"\x02"),
                    (0x31, b"\0"), (0x06, b"\0v00.144\r\n"),
                ])
                transport.read_packet = lambda deadline: next(responses)
                with patch.object(fw.os, "write", side_effect=lambda fd, data: len(data)) as write:
                    self.assertEqual(fw.read_version(transport), "v00.144")
                    self.assertEqual(write.call_count, 1)

    def test_queued_stop_acknowledgement_is_not_ignored_for_update_commands(self):
        for kind in (fw.I2CTransport, preserve.PreservingI2CTransport):
            with self.subTest(transport=kind.__name__):
                transport = object.__new__(kind)
                transport.fd = 123
                transport.emit = lambda _: None
                transport.read_packet = lambda deadline: (0x31, b"\0")
                with patch.object(fw.os, "write", side_effect=lambda fd, data: len(data)) as write:
                    with self.assertRaisesRegex(ValueError, "Unexpected response opcode 0x31"):
                        transport.exchange(0x02, b"", 5)
                    self.assertEqual(write.call_count, 1)

    def test_notifications_do_not_extend_the_command_deadline(self):
        for kind in (fw.I2CTransport, preserve.PreservingI2CTransport):
            with self.subTest(transport=kind.__name__):
                transport = object.__new__(kind)
                transport.fd = 123
                transport.emit = lambda _: None
                transport.read_packet = lambda deadline: (0x8B, b"notification")
                with patch.object(fw.os, "write", side_effect=lambda fd, data: len(data)) as write, \
                        patch.object(fw.time, "monotonic", side_effect=[100, 100.25, 100.5, 101]):
                    with self.assertRaises(TimeoutError):
                        transport.exchange(0x02, b"", 1)
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

    def test_loader_takes_cfg_only_from_sony_package(self):
        images = self.images("cxd5610_updater.efpk")
        cfg = images[-1]
        spec = (cfg[0], len(cfg[1]), hashlib.sha256(cfg[1]).hexdigest(), 3)
        with patch.object(preserve, "load_images", return_value=images[:4]) as code, \
                patch.object(fw, "load_package", return_value=images) as sony, \
                patch.object(fw, "IMAGES", (*fw.IMAGES[:4], spec)):
            self.assertEqual(fw.load_spresense_v201_images(Path("code"), Path("sony.zip")), images)
            code.assert_called_once_with(Path("code"), "v201")
            sony.assert_called_once_with(Path("sony.zip"))

    def test_other_board_cfg_and_changed_sony_cfg_are_rejected(self):
        images = self.images("cxd5610_updater.efpk")
        for cfg in (("other_board.cfg", b"foreign", 3),
                    ("spresense_i2c.cfg", b"changed", 3)):
            with patch.object(preserve, "load_images", return_value=images[:4]), \
                    patch.object(fw, "load_package", return_value=[*images[:4], cfg]):
                with self.assertRaises(ValueError):
                    fw.load_spresense_v201_images(Path("code"), Path("sony.zip"))

    def test_guard_rejects_wrong_cfg_encoding_and_unrelated_commands_before_io(self):
        transport = object.__new__(fw.SpresenseReflashTransport)
        for op, data in ((0, b"\xff"), (9, b"\x03\0\0"), (9, b"\x04\x01\0"),
                         (9, b"\x01\x01\0"), (0x10, b""), (0x32, b"\xef\x3f")):
            with patch.object(fw.os, "write") as write:
                with self.assertRaisesRegex(ValueError, "policy"):
                    transport.exchange(op, data, 1)
                write.assert_not_called()
        self.assertTrue(fw.allowed_spresense_reflash_command(0, b"\0"))
        self.assertTrue(fw.allowed_spresense_reflash_command(9, b"\x03\x01\0"))
        self.assertFalse(preserve.allowed_command(0, b"\0"))
        self.assertFalse(preserve.allowed_command(9, b"\x03\x01\0"))

    def test_offline_cli_identifies_target_and_all_four_program_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv = ["update", "--profile", "v201", "--code-dir", "code",
                    "--package", "sony.zip", "--enter-mode", "erase", "--log-dir", str(root/"plan")]
            with patch.object(sys, "argv", argv), \
                    patch.object(fw, "load_spresense_v201_images", return_value=self.images("cxd5610_updater.efpk")), \
                    patch.object(fw, "SpresenseReflashTransport") as transport, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fw.main(), 0)
                transport.assert_not_called()
            summary = json.loads((root/"plan/summary.json").read_text())
            self.assertEqual(summary["target_version"], "v00.201")
            self.assertFalse(summary["device_opened"])
            self.assertEqual(summary["planned_program_operands"], ["000000", "010000", "020000", "030100"])

    def test_v201_write_requires_explicit_erase_entry_before_input_or_device_access(self):
        argv = ["update", "--profile", "v201", "--code-dir", "code",
                "--package", "sony.zip", "--write", "--gpio-chip", "/dev/gpiochip0"]
        with patch.object(sys, "argv", argv), patch.object(fw, "load_spresense_v201_images") as load, \
                patch.object(fw, "SpresenseReflashTransport") as transport, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                fw.main()
            self.assertEqual(caught.exception.code, 2)
            load.assert_not_called()
            transport.assert_not_called()

    def test_invalid_inputs_stop_before_device_open(self):
        argv = ["update", "--profile", "v201", "--code-dir", "code",
                "--package", "sony.zip", "--write", "--enter-mode", "erase",
                "--gpio-chip", "/dev/gpiochip0"]
        with patch.object(sys, "argv", argv), patch.object(fw.sys, "platform", "linux"), \
                patch.object(fw, "load_spresense_v201_images", side_effect=ValueError("hash mismatch")), \
                patch.object(fw, "SpresenseReflashTransport") as transport:
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                fw.main()
            transport.assert_not_called()

    def test_resume_probes_bootloader_and_never_sends_stop_or_erase(self):
        images = self.images("cxd5610_updater.efpk")

        class Receiver(SimulatedReceiver):
            def exchange(self, op, data, timeout):
                if op == 6 and self.index == 0:
                    self.calls.append((op, data))
                    raise fw.ReceiverCommandError(6, -2)
                response = super().exchange(op, data, timeout)
                if (op, data) == (0, b"\x01"):
                    self.version = "v00.201"
                return response

        receiver = Receiver(images, version="erased")
        receiver.entered = True
        events = []
        self.assertEqual(fw.run_update(receiver, images, "v00.144", events.append,
                                       settle=lambda _: None, enter_mode="erase",
                                       target_version="v00.201", resume_after_erase=True), "v00.201")
        self.assertEqual(receiver.verified_images, [image[0] for image in images])
        self.assertNotIn((0, b"\0"), receiver.calls)
        self.assertNotIn((0x31, b""), receiver.calls)
        self.assertTrue(any(e["event"] == "bootloader_resume_verified" for e in events))

    def test_resume_refuses_normal_firmware_and_unexpected_error_without_image_writes(self):
        for response in (b"\0v00.144", fw.ReceiverCommandError(6, -22), fw.ReceiverCommandError(0, -2)):
            calls = []

            class Receiver:
                def exchange(self, op, data, timeout):
                    calls.append((op, data))
                    if isinstance(response, Exception):
                        raise response
                    return response

            with self.assertRaises(ValueError):
                fw.run_update(Receiver(), self.images("cxd5610_updater.efpk"), "v00.144", lambda _: None,
                              enter_mode="erase", target_version="v00.201", resume_after_erase=True)
            self.assertEqual(calls, [(6, b"")])

    def test_bootloader_high_irq_body_fallback_only_after_wait(self):
        transport = object.__new__(fw.SpresenseReflashTransport)
        transport.bootloader_protocol = True
        transport.active = 1
        transport.irq = 17
        events = []
        transport.emit = events.append
        from unittest.mock import Mock
        transport.request = Mock()
        transport.request.get_value.return_value = 1
        transport.ready = Mock(side_effect=TimeoutError("no new edge"))
        deadline = time.monotonic() + 5
        transport.ready_body(deadline)
        transport.ready.assert_called_once()
        self.assertLess(transport.ready.call_args.args[0], deadline)
        self.assertEqual(events, [{"event": "bootloader_body_level_ready"}])
        transport.request.get_value.return_value = 0
        with self.assertRaises(TimeoutError):
            transport.ready_body(deadline)
        transport.request.get_value.return_value = 1
        with self.assertRaises(TimeoutError):
            transport.ready_body(time.monotonic() - 1)

    def test_normal_firmware_does_not_use_high_irq_body_fallback(self):
        transport = object.__new__(fw.SpresenseReflashTransport)
        transport.bootloader_protocol = False
        transport.ready = lambda _: (_ for _ in ()).throw(TimeoutError("no edge"))
        with self.assertRaises(TimeoutError):
            transport.ready_body(time.monotonic() + 5)

    def test_ff_command_error_is_decoded_before_resume(self):
        transport = object.__new__(fw.I2CTransport)
        transport.fd = 123
        transport.read_packet = lambda _: (0xFF, bytes.fromhex("06fe"))
        with patch.object(fw.os, "write", side_effect=lambda fd, data: len(data)):
            with self.assertRaises(fw.ReceiverCommandError) as caught:
                transport.exchange(6, b"", 5)
        self.assertEqual((caught.exception.opcode, caught.exception.code), (6, -2))


if __name__ == "__main__":
    unittest.main()
