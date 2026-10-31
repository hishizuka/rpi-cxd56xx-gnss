# SPDX-License-Identifier: Apache-2.0
"""Hardware-free mixed-stream, NMEA epoch and POSIX UART lifecycle tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import errno
import os
from pathlib import Path
import pty
import select
import sys
import termios
import threading
import time
import tty
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    import pynmeagps
except ImportError:
    pynmeagps = None

from cxd56xx_gnss._uart_protocol import NMEAState, UARTDecoder, crc24q, decode_qzqsm


def nmea(body):
    body = body.encode("ascii")
    checksum = 0
    for byte in body:
        checksum ^= byte
    return b"$" + body + f"*{checksum:02X}\r\n".encode()


def qzqsm():
    return nmea("QZQSM,55," + "a" * 63) + nmea("QZQSM,56," + "b" * 63)


def rtcm(identity=1077, body=b"example"):
    payload = (identity << 4).to_bytes(2, "big") + body
    header = b"\xd3" + len(payload).to_bytes(2, "big")
    return header + payload + crc24q(header + payload).to_bytes(3, "big")


def binary(identity=0x8B, payload=b"example"):
    header = b"\x7f" + len(payload).to_bytes(2, "little") + bytes((identity,))
    result = header + bytes((sum(header) & 255,)) + payload
    return result + bytes((sum(payload) & 255,)) if payload else result


def epoch(clock="120000.00", status="A", modes=(2, 3)):
    result = [nmea(f"GNGGA,{clock},3500.000,N,13900.000,E,1,12,0.8,50.0,M,30.0,M,,")]
    result += [nmea(f"GNGSA,A,{mode},01,02,,,,,,,,,,,1.2,0.8,0.9,1") for mode in modes]
    result += [
        nmea("GPGSV,1,1,02,01,30,100,40,02,20,120,35,1"),
        nmea("GPGSV,1,1,02,01,30,100,40,02,20,120,35,5"),
        nmea("GQGSV,1,1,01,193,30,100,40,1"),
        nmea(f"GNRMC,{clock},{status},3500.000,N,13900.000,E,10.0,123.0,301026,,,A,V"),
    ]
    return result


def until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.002)
    raise AssertionError("Condition did not become true")


class FramingTests(unittest.TestCase):
    def test_mixed_frames_at_every_chunk_boundary(self):
        frames = [
            nmea("GNTXT,01,01,00,example"),
            rtcm(body=b"\n[GSR] Done\r\n$GNbad*00\r\n\x7f"),
            binary(payload=b"\n[GSR] Done\r\n\xd3"),
            b"v00.201\r\n",
            b"[VER] Done\r\n",
        ]
        stream = b"".join(frames)
        for step in range(1, len(stream) + 1):
            decoder = UARTDecoder()
            result = []
            for index in range(0, len(stream), step):
                result += decoder.feed(stream[index : index + step])
            self.assertEqual([item[1] for item in result], frames)
            self.assertEqual(
                [item[0] for item in result], ["nmea", "rtcm", "binary", "text", "text"]
            )
            self.assertFalse(decoder.errors)

    def test_checksums_and_resynchronization(self):
        self.assertEqual(crc24q(b"123456789"), 0xCDE703)
        corrupt = rtcm()[:-1] + bytes((rtcm()[-1] ^ 1,))
        packet = binary()
        corrupt_binary = packet[:-1] + bytes((packet[-1] ^ 1,))
        decoder = UARTDecoder()
        frames = decoder.feed(
            b"\0\1$GNTXT,broken*00\r\n" + corrupt + corrupt_binary + rtcm(1097)
        )
        self.assertEqual(
            [raw for protocol, raw, _ in frames if protocol == "rtcm"], [rtcm(1097)]
        )
        self.assertEqual(decoder.errors["rtcm_crc"], 1)
        self.assertEqual(decoder.errors["binary_checksum"], 1)
        self.assertGreater(decoder.errors["nmea_checksum"], 0)

    def test_qzqsm_preserves_bits_without_crc_reconstruction(self):
        data = "0123456789abcdef" * 3 + "0123456789abcde"
        packet = nmea("QZQSM,55," + data)
        report = decode_qzqsm(packet, 123.5)
        self.assertEqual(report.satellite_id, 55)
        self.assertEqual(report.data, bytes.fromhex(data + "0"))
        self.assertEqual(report.received_at, 123.5)
        self.assertEqual(report.to_azarashi_packet(), b"7" + report.data)
        for invalid in ("0" * 62, " " * 63, "g" * 63):
            with self.assertRaises(ValueError):
                decode_qzqsm(nmea("QZQSM,55," + invalid), 0)


@unittest.skipUnless(pynmeagps is not None, "Install the uart extra for NMEA tests")
class PositionTests(unittest.TestCase):
    def parse(self, lines):
        state = NMEAState()
        return [value for raw in lines if (value := state.observe(raw)) is not None]

    def test_epoch_units_modes_and_dual_signal_satellite_union(self):
        (position,) = self.parse(epoch())
        self.assertEqual((position.lat, position.lon), (35, 139))
        self.assertEqual((position.alt, position.geoid_separation), (50, 30))
        self.assertAlmostEqual(position.speed, 10 * 1852 / 3600)
        self.assertEqual(position.track, 123)
        self.assertEqual(position.mode, 3)
        self.assertEqual(position.status, 1)
        self.assertEqual((position.pdop, position.hdop, position.vdop), (1.2, 0.8, 0.9))
        self.assertEqual((position.used_sats, position.total_sats), (12, 3))
        self.assertEqual(
            position.timestamp,
            datetime(2026, 10, 30, 12, tzinfo=timezone.utc).timestamp(),
        )

    def test_missing_gsa_does_not_invent_3d(self):
        (position,) = self.parse(epoch(modes=()))
        self.assertIsNone(position.mode)
        self.assertIsNone(position.vdop)
        self.assertEqual(position.alt, 50)

    def test_no_fix_clears_position_and_motion(self):
        fixed, missing = self.parse(epoch() + epoch("120001.00", "V"))
        self.assertEqual(fixed.mode, 3)
        self.assertEqual(missing.mode, 1)
        for name in ("lat", "lon", "alt", "speed", "track", "timestamp"):
            self.assertIsNone(getattr(missing, name))

    def test_missing_or_mismatched_epoch_does_not_reuse_gga(self):
        lines = epoch()
        first, second = self.parse(lines + [lines[-1]])
        self.assertEqual(first.alt, 50)
        self.assertIsNone(second.alt)
        self.assertIsNone(second.mode)
        (mismatched,) = self.parse(lines[:-1] + epoch("120001.00")[-1:])
        self.assertIsNone(mismatched.alt)
        self.assertIsNone(mismatched.total_sats)

    def test_truncated_sentences_do_not_fail_or_reuse_previous_mode(self):
        lines = [nmea("GNGGA,120000.00"), nmea("GNGSA,A,3"), epoch()[-1]]
        (position,) = self.parse(lines)
        self.assertIsNone(position.alt)
        self.assertIsNone(position.mode)

    def test_partial_or_conflicting_gsv_does_not_invent_total(self):
        for extra in (
            nmea("GAGSV,2,1,05,10,30,100,40,1"),
            nmea("GAGSV,2,2,05,11,30,100,40,1"),
            nmea("GNGSV,1,1,01,01,30,100,40,1"),
        ):
            lines = epoch()
            (position,) = self.parse(lines[:-1] + [extra, lines[-1]])
            self.assertIsNone(position.total_sats)


class Receiver:
    """A synthetic receiver behind a real PTY; no physical hardware is used."""

    def __init__(self):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.device = os.ttyname(self.slave)
        self.commands = []
        self.mask = 0x1FCB
        self.hook = None
        self.stopping = threading.Event()
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def send(self, data):
        os.write(self.master, data)

    def run(self):
        pending = bytearray()
        while not self.stopping.is_set():
            ready, _, _ = select.select([self.master], [], [], 0.02)
            if not ready:
                continue
            try:
                pending.extend(os.read(self.master, 4096))
            except OSError:
                return
            while b"\n" in pending:
                raw, _, rest = pending.partition(b"\n")
                pending[:] = rest
                line = raw.strip().decode("ascii")
                self.commands.append(line)
                if self.hook is not None and self.hook(line):
                    continue
                name = line.split()[0][1:]
                if name == "GGNS":
                    self.send(f"0x{self.mask:08X}\r\n".encode())
                if name == "GNS":
                    self.mask = int(line.split()[1], 0)
                self.send(f"[{name}] Done\r\n".encode())

    def close(self):
        self.stopping.set()
        self.worker.join(2)
        os.close(self.slave)
        os.close(self.master)


@unittest.skipUnless(pynmeagps is not None, "Install the uart extra for UART tests")
class UARTTests(unittest.TestCase):
    def setUp(self):
        from cxd56xx_gnss import CXD56xxUART, CXD56xxSessionError, UARTCommandError

        self.driver = CXD56xxUART
        self.error = CXD56xxSessionError
        self.rejected = UARTCommandError
        self.receiver = Receiver()
        self.addCleanup(self.receiver.close)

    def open(self, **kwargs):
        gps = self.driver(self.receiver.device, command_timeout=0.3, **kwargs)
        self.addCleanup(gps.close)
        return gps

    def test_constructor_is_passive_and_does_not_flush_or_reset(self):
        self.receiver.send(b"".join(epoch()))
        gps = self.open()
        until(lambda: gps.peek() == 0)
        self.assertEqual(gps.lat, 35)
        self.assertEqual(self.receiver.commands, [])
        self.assertFalse(termios.tcgetattr(self.receiver.slave)[2] & termios.HUPCL)
        gps.close()
        self.assertEqual(self.receiver.commands, [])
        self.assertEqual(gps.session_status.state, "closed")
        with self.assertRaises(ValueError):
            gps.peek()

    def test_bounded_wake_then_idle_start(self):
        wakes = []

        def hook(command):
            if command == "@WUP":
                wakes.append(command)
                return len(wakes) < 3
            return False

        self.receiver.hook = hook
        gps = self.open()
        with patch.object(gps, "WAKE_TIMEOUT", 0.02):
            self.assertTrue(gps.start())
        self.assertEqual(self.receiver.commands, ["@WUP"] * 3 + ["@GSTP", "@GSR"])
        self.assertEqual(gps.peek(), -errno.EAGAIN)

    def test_rtcm_without_fix_and_independent_queues(self):
        gps = self.open(queue_size=2)
        self.assertTrue(gps.set_rtcm_mask(0x07102202))
        gps.read_frames()
        self.assertEqual(gps.session_status.rtcm_mask, 0x07102202)
        payloads = [rtcm(1077), rtcm(1097), rtcm(1117)]
        self.receiver.send(b"".join(payloads))
        until(lambda: gps.dropped["rtcm"] == 1)
        frames = gps.read_rtcm()
        self.assertEqual([frame.data for frame in frames], payloads[1:])
        self.assertEqual([frame.message_type for frame in frames], [1097, 1117])
        self.assertGreater(frames[0].received_at, 0)
        self.assertEqual(gps.read_rtcm(), [])
        self.assertEqual(len(gps.read_frames()), 2)
        self.assertEqual(gps.peek(), -errno.EAGAIN)
        gps.set_rtcm_mask(0)
        self.assertEqual(
            self.receiver.commands[-4:], ["@WUP", "@GSTP", "@RTCM 0x0", "@GSR"]
        )

    def test_l1s_changes_only_tracking_bit_and_checks_readback(self):
        gps = self.open()
        self.assertEqual(gps.get_satellite_mask(), 0x1FCB)
        self.assertTrue(gps.set_l1s_enabled(True))
        self.assertEqual(gps.session_status.satellite_mask, 0x1FEB)
        self.assertTrue(gps.set_l1s_enabled(False))
        self.assertEqual(self.receiver.mask, 0x1FCB)
        self.assertFalse(any("BSSL" in command for command in self.receiver.commands))

    def test_synthetic_qzqsm_reaches_existing_dcreport_api(self):
        gps = self.open()
        data = "a" * 63
        self.receiver.send(nmea("QZQSM,55," + data))
        reports = until(gps.read_dcreports)
        self.assertEqual(reports[0].data, bytes.fromhex(data + "0"))
        self.assertEqual(gps.read_dcreports(), [])
        self.assertEqual(gps.peek(), -errno.EAGAIN)

    def test_dcr_configuration_and_received_objects_match_the_i2c_api(self):
        from cxd56xx_gnss import DCReport

        gps = self.open(qzss_dcr_enabled=True)
        self.assertEqual(
            self.receiver.commands,
            [
                "@WUP",
                "@GSTP",
                "@GGNS",
                "@GNS 0x1feb",
                "@GGNS",
                "@BSSL 0xe0f7fef",
                "@GSR",
            ],
        )
        self.assertIs(gps.session_status.qzss_dcr_requested, True)
        self.assertIs(gps.session_status.qzss_dcr_applied, True)
        self.receiver.send(qzqsm())
        reports = []
        until(lambda: (reports.extend(gps.read_dcreports()), len(reports) == 2)[1])
        self.assertTrue(all(isinstance(report, DCReport) for report in reports))
        self.assertEqual([report.satellite_id for report in reports], [55, 56])
        self.assertEqual(gps.peek(), -errno.EAGAIN)
        self.assertEqual(gps.dcreports_dropped, 0)

    def test_disable_discards_queued_and_late_reports_and_can_be_reenabled(self):
        gps = self.open(qzss_dcr_enabled=True)
        self.receiver.send(qzqsm())
        until(lambda: any(frame.message_type == "QZQSM" for frame in gps.read_frames()))
        self.assertTrue(gps.set_qzss_dcr_enabled(False))
        self.assertEqual(self.receiver.mask, 0x1FCB)
        self.assertEqual(self.receiver.commands[-2:], ["@BSSL 0xef", "@GSR"])
        self.assertIs(gps.session_status.qzss_dcr_applied, False)
        self.assertEqual(gps.read_dcreports(), [])
        self.receiver.send(qzqsm())
        until(lambda: any(frame.message_type == "QZQSM" for frame in gps.read_frames()))
        self.assertEqual(gps.read_dcreports(), [])
        self.assertTrue(gps.set_qzss_dcr_enabled(True))
        self.receiver.send(qzqsm())
        self.assertTrue(until(gps.read_dcreports))

    def test_dcr_change_does_not_publish_command_time_reports(self):
        def hook(command):
            if command.startswith("@BSSL"):
                self.receiver.send(qzqsm())
            return False

        self.receiver.hook = hook
        gps = self.open(qzss_dcr_enabled=True)
        self.assertEqual(gps.read_dcreports(), [])
        self.receiver.send(qzqsm())
        self.assertTrue(until(gps.read_dcreports))

    def test_partial_dcr_configuration_rejection_is_terminal(self):
        for name in ("GNS", "BSSL", "GSR"):
            with self.subTest(command=name):
                self.receiver.commands.clear()

                def hook(command):
                    if command.split()[0] == "@" + name:
                        self.receiver.send(f"[{name}] Err -22\r\n".encode())
                        return True
                    return False

                self.receiver.hook = hook
                gps = self.open()
                with self.assertRaises(self.error) as caught:
                    gps.set_qzss_dcr_enabled(True)
                self.assertEqual(caught.exception.stage, "qzss_dcr")
                self.assertEqual(gps.session_status.state, "failed")
                self.assertIs(gps.session_status.qzss_dcr_requested, True)
                self.assertIsNone(gps.session_status.qzss_dcr_applied)
                with self.assertRaises(self.error):
                    gps.read_dcreports()
                gps.close()

    def test_constructor_dcr_failure_releases_the_uart(self):
        def hook(command):
            if command.startswith("@BSSL"):
                self.receiver.send(b"[BSSL] Err -22\r\n")
                return True
            return False

        self.receiver.hook = hook
        with self.assertRaises(self.error):
            self.driver(
                self.receiver.device, qzss_dcr_enabled=True, command_timeout=0.3
            )
        self.receiver.hook = None
        gps = self.open()
        self.assertTrue(gps.set_qzss_dcr_enabled(False))

    def test_dcr_drop_counter_and_direct_tracking_change(self):
        gps = self.open(qzss_dcr_enabled=True, queue_size=1)
        self.receiver.send(qzqsm())
        until(lambda: gps.dcreports_dropped == 1)
        self.assertEqual(len(gps.read_dcreports()), 1)
        gps.set_l1s_enabled(False)
        self.assertIsNone(gps.session_status.qzss_dcr_applied)
        self.receiver.send(qzqsm())
        until(lambda: any(frame.message_type == "QZQSM" for frame in gps.read_frames()))
        self.assertEqual(gps.read_dcreports(), [])

    def test_definite_rejection_preserves_session(self):
        def hook(command):
            if command.startswith("@RTCM"):
                self.receiver.send(b"[RTCM] Err -22\r\n")
                return True
            return False

        self.receiver.hook = hook
        gps = self.open()
        with self.assertRaises(self.rejected) as caught:
            gps.set_rtcm_mask(2)
        self.assertEqual(caught.exception.command, "RTCM")
        self.assertEqual(caught.exception.response, "[RTCM] Err -22")
        self.assertIsNone(gps.session_status.rtcm_mask)
        self.assertEqual(gps.get_satellite_mask(), 0x1FCB)
        gps.start()

    def test_ack_inside_binary_is_not_a_command_reply(self):
        def hook(command):
            if command == "@GSR":
                self.receiver.send(rtcm(body=b"[GSR] Done\r\n"))
                return True
            return False

        self.receiver.hook = hook
        gps = self.open()
        with self.assertRaises(self.error) as caught:
            gps.start()
        self.assertEqual(caught.exception.errno, errno.ETIMEDOUT)
        self.assertEqual(caught.exception.stage, "command:GSR")
        for method in (gps.peek, gps.read_rtcm, gps.start):
            with self.assertRaises(self.error):
                method()
        self.assertEqual(gps.session_status.state, "failed")

    def test_no_wake_ack_fails_with_bounded_retries(self):
        self.receiver.hook = lambda command: True
        gps = self.open()
        with (
            patch.object(gps, "WAKE_TIMEOUT", 0.02),
            patch.object(gps, "WAKE_ATTEMPTS", 2),
        ):
            with self.assertRaises(self.error) as caught:
                gps.start()
        self.assertEqual(caught.exception.stage, "wake")
        self.assertEqual(self.receiver.commands, ["@WUP", "@WUP"])

    def test_readback_mismatch_is_terminal(self):
        def hook(command):
            if command.startswith("@GNS "):
                self.receiver.send(b"[GNS] Done\r\n")
                return True
            return False

        self.receiver.hook = hook
        gps = self.open()
        with self.assertRaises(self.error) as caught:
            gps.set_l1s_enabled(True)
        self.assertEqual(caught.exception.stage, "GNS")
        self.assertNotIn("@GSR", self.receiver.commands)

    def test_close_waits_for_inflight_configuration(self):
        entered, release = threading.Event(), threading.Event()

        def hook(command):
            if command == "@RTCM 0x2":
                entered.set()
                release.wait(2)
            return False

        self.receiver.hook = hook
        gps = self.open()
        with ThreadPoolExecutor(2) as pool:
            changing = pool.submit(gps.set_rtcm_mask, 2)
            self.assertTrue(entered.wait(1))
            closing = pool.submit(gps.close)
            time.sleep(0.02)
            self.assertFalse(closing.done())
            release.set()
            self.assertTrue(changing.result(timeout=1))
            closing.result(timeout=1)
        self.assertEqual(self.receiver.commands[-1], "@GSR")
        self.assertEqual(gps.session_status.state, "closed")

    def test_transport_failure_is_terminal_and_releases_port(self):
        gps = self.open()
        with patch(
            "cxd56xx_gnss._uart.os.read", side_effect=OSError(errno.EIO, "test failure")
        ):
            self.receiver.send(rtcm())
            until(lambda: gps.session_status.state == "failed")
        with self.assertRaises(self.error) as caught:
            gps.read_rtcm()
        self.assertEqual(caught.exception.stage, "transport")
        gps.close()
        other = self.open()
        other.start()

    def test_advisory_exclusion_and_validation(self):
        gps = self.open()
        with self.assertRaises(BlockingIOError):
            self.open()
        for value in (-1, 1 << 27, True):
            with self.assertRaises(ValueError):
                gps.set_rtcm_mask(value)
        with self.assertRaises(TypeError):
            gps.set_l1s_enabled(1)
        with self.assertRaises(TypeError):
            gps.set_qzss_dcr_enabled(1)
        with self.assertRaises(TypeError):
            self.driver(self.receiver.device, qzss_dcr_enabled=1)
        for kwargs in (
            {"baudrate": 12345},
            {"queue_size": 0},
            {"command_timeout": float("nan")},
        ):
            with self.assertRaises(ValueError):
                self.driver(self.receiver.device, **kwargs)


if __name__ == "__main__":
    unittest.main()
