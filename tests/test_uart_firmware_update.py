# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
import io
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import uart_firmware_update as fw
from i2c_firmware_update import read_version as read_binary_version


class Receiver:
    def __init__(self, normal_after_ram=False, fail_slot=None):
        self.calls = []
        self.ram = False
        self.booted = False
        self.normal_after_ram = normal_after_ram
        self.fail_slot = fail_slot

    def read_version(self):
        return read_binary_version(self)

    def stop(self):
        self.exchange(0x31, b"", 5)

    def force_boot(self, timeout):
        self.exchange(0, b"\xff", timeout)

    def exchange(self, opcode, payload, timeout):
        if not fw.allowed_command(opcode, payload):
            raise AssertionError("Forbidden command reached receiver")
        self.calls.append((opcode, payload))
        if opcode == 6:
            if self.ram and not self.booted and not self.normal_after_ram:
                raise TimeoutError("Updater does not implement version query")
            return b"\0v00.144" if self.booted else b"\0v00.129"
        if opcode == 2:
            self.ram = True
        if opcode == 9 and payload[0] == self.fail_slot:
            return b"\xea"
        if (opcode, payload) == (0, b"\x01"):
            self.booted = True
            return b"\x01"
        return b"\0"


class UARTFirmwareTests(unittest.TestCase):
    images = [("updater.efpk", b"u", None), ("cxd5610_sdk.efpk", b"s", 0),
              ("cxd5610_app.efpk", b"a", 1), ("cxd5610_lib.efpk", b"l", 2)]

    def run_sequence(self, receiver, resume_bootloader=False):
        with patch.object(fw, "validate_images"):
            return fw.run_update(receiver, self.images, "v00.129", lambda _: None,
                                 settle=lambda _: None, resume_bootloader=resume_bootloader)

    def test_policy_blocks_erase_config_unknown_commands_and_invalid_frames(self):
        for opc, data in [(0, b"\0"), (9, b"\x03\x01\0"),
                          (9, b"\0\x01\0"), (0x0A, b""), (1, b"\x01\0\0\0x")]:
            self.assertFalse(fw.allowed_command(opc, data))
        self.assertTrue(fw.allowed_command(0, b"\xff"))

    def test_unexpected_image_set_rejected_before_commands(self):
        with self.assertRaises(ValueError):
            fw.validate_images(self.images + [("config.cfg", b"c", 3)])

    def test_success_writes_exactly_three_non_config_slots_and_checks_version(self):
        receiver = Receiver()
        self.assertEqual(self.run_sequence(receiver), "v00.144")
        self.assertEqual([data for opc, data in receiver.calls if opc == 9],
                         [b"\0\0\0", b"\x01\0\0", b"\x02\0\0"])
        self.assertNotIn((0, b"\0"), receiver.calls)

    def test_resume_bootloader_starts_with_ram_and_never_reenters_or_erases(self):
        receiver = Receiver()
        self.assertEqual(self.run_sequence(receiver, resume_bootloader=True), "v00.144")
        self.assertEqual(receiver.calls[0][0], 1)
        self.assertNotIn((0, b"\xff"), receiver.calls)
        self.assertNotIn((0, b"\0"), receiver.calls)
        self.assertNotIn((0x31, b""), receiver.calls)
        self.assertEqual([payload[0] for opc, payload in receiver.calls if opc == 9], [0, 1, 2])

    def test_missing_boot_ack_requires_independent_target_version_confirmation(self):
        receiver = Receiver()
        exchange = receiver.exchange

        def lost_boot_ack(opcode, payload, timeout):
            response = exchange(opcode, payload, timeout)
            if (opcode, payload) == (0, b"\x01"):
                raise TimeoutError("Boot acknowledgement lost during UART switch")
            return response

        receiver.exchange = lost_boot_ack
        self.assertEqual(self.run_sequence(receiver), "v00.144")
        self.assertEqual(receiver.calls[-1], (6, b""))

    def test_missing_boot_ack_does_not_hide_a_failed_version_check(self):
        receiver = Receiver()
        exchange = receiver.exchange

        def failed_boot(opcode, payload, timeout):
            if (opcode, payload) == (0, b"\x01"):
                receiver.calls.append((opcode, payload))
                raise TimeoutError("Boot did not complete")
            if opcode == 6 and receiver.ram:
                return b"\0v00.129"
            return exchange(opcode, payload, timeout)

        receiver.exchange = failed_boot
        # The RAM-stage probe must time out; only the final query returns v129.
        reads = 0

        def read_version():
            nonlocal reads
            reads += 1
            if reads == 2:
                raise TimeoutError("Updater has no ASCII version query")
            return read_binary_version(receiver)

        receiver.read_version = read_version
        with self.assertRaisesRegex(ValueError, "Expected v00.144"):
            self.run_sequence(receiver)

    def test_normal_firmware_after_ram_blocks_all_nonvolatile_writes(self):
        receiver = Receiver(normal_after_ram=True)
        with self.assertRaisesRegex(RuntimeError, "physical-reset"):
            self.run_sequence(receiver)
        self.assertFalse(any(opc == 9 for opc, _ in receiver.calls))
        self.assertNotIn((0, b"\x01"), receiver.calls)

    def test_failed_write_does_not_retry_continue_or_reset(self):
        receiver = Receiver(fail_slot=1)
        with self.assertRaisesRegex(ValueError, "rejected"):
            self.run_sequence(receiver)
        self.assertEqual(receiver.calls[-1], (9, b"\x01\0\0"))
        self.assertNotIn((0, b"\x01"), receiver.calls)

    def test_fragmented_uart_response_and_leading_nmea(self):
        class Serial:
            def __init__(self, parts):
                self.parts = iter(parts)

            @property
            def in_waiting(self):
                return 0

            def read(self, count):
                return next(self.parts)

        transport = object.__new__(fw.UARTTransport)
        transport.buffer = bytearray()
        transport.raw = io.BytesIO()
        transport.serial = Serial([b"$GPGGA,noise\r\n\x7f", b"\x01\x00", b"\x01\x81\x00", b"\x00"])
        self.assertEqual(transport.read_packet(fw.time.monotonic() + 1), (1, b"\0"))

    def test_corrupt_uart_ack_is_not_accepted(self):
        transport = object.__new__(fw.UARTTransport)
        transport.buffer = bytearray.fromhex("7f010001810001")
        with self.assertRaisesRegex(ValueError, "checksum"):
            transport.read_packet(fw.time.monotonic() + 1)

    def test_transport_policy_rejects_erase_before_any_uart_write(self):
        transport = object.__new__(fw.UARTTransport)
        with self.assertRaisesRegex(ValueError, "forbidden"):
            transport.exchange(0, b"\0", 1)

    def test_force_boot_candidate_sends_no_erase(self):
        transport = object.__new__(fw.UARTTransport)
        transport.emit = lambda _: None
        replies = iter([b"\x01", b"\0"])
        calls = []

        def exchange(opcode, payload, timeout):
            calls.append((opcode, payload))
            return next(replies)

        transport.exchange = exchange
        transport.force_boot(1)
        self.assertEqual(calls, [(0, b"\xff"), (0, b"\xff")])

    def test_known_boot_time_command_error_only_retries_force_request(self):
        transport = object.__new__(fw.UARTTransport)
        events = []
        transport.emit = events.append
        replies = iter([fw.ReceiverCommandError(0, -2), b"\0"])
        calls = []

        def exchange(opcode, payload, timeout):
            calls.append((opcode, payload))
            response = next(replies)
            if isinstance(response, Exception):
                raise response
            return response

        transport.exchange = exchange
        transport.force_boot(1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call == (0, b"\xff") for call in calls))
        self.assertTrue(any(e['event'] == 'force_entry_not_ready' for e in events))

    def test_unconfirmed_ram_transfer_blocks_execution_and_all_program_writes(self):
        receiver = Receiver()
        exchange = receiver.exchange

        def fail_injection(opcode, payload, timeout):
            if opcode == 1:
                receiver.calls.append((opcode, payload))
                raise TimeoutError("No RAM injection acknowledgement")
            return exchange(opcode, payload, timeout)

        receiver.exchange = fail_injection
        with self.assertRaises(TimeoutError):
            self.run_sequence(receiver)
        self.assertFalse(any(opc in (2, 9) for opc, _ in receiver.calls))

    def test_force_rejection_stops_without_retrying_a_different_mode(self):
        transport = object.__new__(fw.UARTTransport)
        transport.emit = lambda _: None
        calls = []

        def exchange(opcode, payload, timeout):
            calls.append((opcode, payload))
            return b"\xea"

        transport.exchange = exchange
        with self.assertRaisesRegex(ValueError, "rejected"):
            transport.force_boot(1)
        self.assertEqual(calls, [(0, b"\xff")])


if __name__ == "__main__":
    unittest.main()
