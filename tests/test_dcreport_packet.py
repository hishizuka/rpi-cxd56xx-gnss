# SPDX-License-Identifier: Apache-2.0
"""Test the public packet adapter without opening or importing native devices."""

import importlib.util
import json
from pathlib import Path
import sys
import unittest

spec = importlib.util.spec_from_file_location(
    "cxd56xx_packet_types", Path(__file__).resolve().parents[1] / "src/cxd56xx_gnss/_types.py"
)
types = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = types
spec.loader.exec_module(types)
DCReport = types.DCReport

FIXTURES = json.loads((Path(__file__).parent / "data" / "dcreport-synthetic.json").read_text())
INVALID = bytes.fromhex(FIXTURES["crc_vectors"]["invalid_data_hex"])
VALID = bytes.fromhex(FIXTURES["crc_vectors"]["valid_data_hex"])


class PacketTests(unittest.TestCase):
    def test_default_preserves_received_frame_and_three_field_contract(self):
        message = DCReport(56, INVALID, 1234.5)
        self.assertEqual(message._fields, ("satellite_id", "data", "received_at"))
        self.assertEqual(message.to_azarashi_packet(), b"\x38" + INVALID)
        self.assertEqual(tuple(message), (56, INVALID, 1234.5))

    def test_explicit_reconstruction_matches_known_crc(self):
        message = DCReport(56, INVALID, 1234.5)
        packet = message.to_azarashi_packet(reconstruct_crc=True)
        self.assertIsInstance(packet, bytes)
        self.assertEqual(len(packet), 33)
        self.assertEqual(packet[0], 56)
        self.assertEqual((int.from_bytes(packet[1:], "big") >> 6) & 0xFFFFFF, 0xB9FC68)
        self.assertEqual((int.from_bytes(message.data, "big") >> 6) & 0xFFFFFF, 0x707478)
        self.assertIs(message.data, INVALID)
        self.assertEqual(message.received_at, 1234.5)

    def test_payload_and_every_padding_value_are_retained(self):
        for padding in range(64):
            with self.subTest(padding=padding):
                raw = INVALID[:-1] + bytes(((INVALID[-1] & 0xC0) | padding,))
                message = DCReport(61, raw, 1234.5)
                body = message.to_azarashi_packet(reconstruct_crc=True)[1:]
                self.assertEqual(int.from_bytes(raw, "big") >> 30,
                                 int.from_bytes(body, "big") >> 30)
                self.assertEqual(body[-1] & 63, padding)
                self.assertEqual(message.data, raw)

    def test_valid_received_frame_is_not_changed_by_reconstruction(self):
        message = DCReport(61, VALID, 1234.5)
        self.assertEqual(message.to_azarashi_packet(reconstruct_crc=True), b"\x3d" + VALID)

    def test_reconstruction_requires_an_explicit_boolean(self):
        message = DCReport(56, INVALID, 1234.5)
        for value in ("False", 1, None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                message.to_azarashi_packet(reconstruct_crc=value)

    def test_malformed_manually_constructed_reports_are_rejected(self):
        for message in (DCReport(0, INVALID, 0), DCReport(256, INVALID, 0),
                        DCReport(56, b"short", 0)):
            with self.subTest(message=message), self.assertRaises(ValueError):
                message.to_azarashi_packet(reconstruct_crc=True)


if __name__ == "__main__":
    unittest.main()
