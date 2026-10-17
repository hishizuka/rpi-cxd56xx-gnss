# SPDX-License-Identifier: Apache-2.0
"""Validate RTCM framing and extraction without hardware."""

from pathlib import Path
import argparse
import sys
import signal
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import i2c_rtcm_test as probe


def frame(payload):
    header = b"\xd3" + len(payload).to_bytes(2, "big")
    message = header + payload
    return message + probe.crc24q(message).to_bytes(3, "big")


def outer(opc, payload):
    header = b"\x7f" + len(payload).to_bytes(2, "little") + bytes([opc])
    return header + bytes([sum(header) % 256]) + payload + (bytes([sum(payload) % 256]) if payload else b"")


class RtcmTests(unittest.TestCase):
    def test_sony_mask_matches_documented_wire_bytes(self):
        mask = probe.parse_rtcm_mask("0x07102202")
        self.assertEqual(mask, (1 << 1) | (1 << 9) | (1 << 13) | (1 << 20) | (1 << 24) | (1 << 25) | (1 << 26))
        self.assertEqual(mask.to_bytes(4, "little"), bytes.fromhex("02 22 10 07"))

    def test_gnss_selection_excludes_reserved_bits(self):
        self.assertEqual(probe.parse_gnss_mask("0x3fef"), 0x3fef)
        for value in ("0", "0x10", "0xffff", "-1"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                probe.parse_gnss_mask(value)

    def test_invalid_mask_is_rejected(self):
        for value in ("-1", "0x100000000", "invalid"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                probe.parse_rtcm_mask(value)

    def test_standard_crc24q_check_value(self):
        self.assertEqual(probe.crc24q(b"123456789"), 0xCDE703)

    def test_valid_frame_and_message_type(self):
        message = frame(bytes([1077 >> 4, (1077 & 15) << 4, 0]))
        frames, result = probe.split_rtcm(message)
        self.assertEqual(frames, [message])
        self.assertEqual(result, {"crc_errors": 0, "discarded_bytes": 0, "trailing_bytes": 0})

    def test_corrupt_frame_is_rejected_and_next_frame_recovers(self):
        valid = frame(b"\x43\x50\x00")
        corrupt = bytearray(valid)
        corrupt[-1] ^= 1
        frames, result = probe.split_rtcm(bytes(corrupt) + valid)
        self.assertEqual(frames, [valid])
        self.assertEqual(result["crc_errors"], 1)
        self.assertGreater(result["discarded_bytes"], 0)

    def test_truncated_frame_is_reported(self):
        message = frame(b"\x43\x50\x00")
        frames, result = probe.split_rtcm(message[:-1])
        self.assertEqual(frames, [])
        self.assertEqual(result["trailing_bytes"], len(message) - 1)

    def test_rtcm_split_across_notifications(self):
        message = frame(b"\x43\x50\x00")
        parts = [message[:4], message[4:]]
        data = b"".join(outer(0x84, b"\x00" + len(part).to_bytes(2, "little") + bytes([2, index]) + part) for index, part in enumerate(parts, 1))
        stream, counts, metadata = probe.decode_capture(outer(0x45, b"\x00") + data)
        self.assertEqual(stream, message)
        self.assertEqual(counts, {"0x45": 1, "0x84": 2})
        self.assertEqual([item["serial"] for item in metadata], [1, 2])
        self.assertEqual(probe.split_rtcm(stream)[0], [message])

    def test_invalid_outer_checksum_is_rejected(self):
        packet = bytearray(outer(0x84, b"\x00" * 5))
        packet[-1] ^= 1
        with self.assertRaises(ValueError):
            probe.decode_capture(packet)

    def test_spresense_v144_uses_a_three_byte_envelope(self):
        message = frame(b"\x43\x50\x00")
        payload = b"\x80" + len(message).to_bytes(2, "little") + message
        stream, counts, metadata = probe.decode_capture(outer(0x84, payload))
        self.assertEqual(stream, message)
        self.assertEqual(counts, {"0x84": 1})
        self.assertEqual(metadata[0]["format"], "spresense-v144")
        self.assertEqual(metadata[0]["header_bytes"], 3)
        self.assertIsNone(metadata[0]["serial"])
        self.assertEqual(probe.split_rtcm(stream)[0], [message])

    def test_truncated_outer_packet_is_rejected(self):
        with self.assertRaises(ValueError):
            probe.decode_capture(outer(0x84, b"\x00" * 5)[:-1])

    def test_rejected_notification_is_separate_from_successful_cleanup(self):
        events = [
            {"event": "command", "opc": 0x45, "result": 0, "request_hex": "2222f207"},
            {"event": "command", "opc": 0x34, "result": -22, "request_hex": "84"},
            {"event": "command", "opc": 0x34, "result": 0, "request_hex": "8081828389"},
            {"event": "cleanup", "stop": 0, "disable_rtcm": 0, "normal_notify": 0},
        ]
        summary = probe.summarize_events(events)
        self.assertFalse(summary["rtcm_notification_accepted"])
        self.assertFalse(summary["capture_started"])
        self.assertTrue(summary["normal_notifications_restored"])
        self.assertEqual(len(summary["command_errors"]), 1)

    def test_clean_interrupt_requires_successful_cleanup(self):
        summary = {"returncode": 130, "stop_signal": signal.SIGINT,
                   "normal_notifications_restored": True, "valid_rtcm_frames": 3}
        self.assertEqual(probe.capture_exit_code(summary), 130)
        summary["normal_notifications_restored"] = False
        self.assertEqual(probe.capture_exit_code(summary), 1)

    def test_clean_sigterm_and_timeout_are_distinct(self):
        summary = {"returncode": 130, "stop_signal": signal.SIGTERM,
                   "normal_notifications_restored": True}
        self.assertEqual(probe.capture_exit_code(summary), 143)
        del summary["stop_signal"]
        self.assertEqual(probe.capture_exit_code(summary), 1)

    def test_interrupted_capture_with_corruption_fails(self):
        self.assertEqual(probe.capture_exit_code({
            "returncode": 130, "stop_signal": signal.SIGINT,
            "normal_notifications_restored": True, "crc_errors": 1,
        }), 1)

    def test_firmware_success_prefix_is_removed(self):
        summary = probe.summarize_events([
            {"event": "command", "opc": 0x06, "result": 0, "payload_hex": "007630302e3130330d0a"},
        ])
        self.assertEqual(summary["firmware_reply_ascii"], "v00.103")


if __name__ == "__main__":
    unittest.main()
