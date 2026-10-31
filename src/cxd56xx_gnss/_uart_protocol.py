# SPDX-License-Identifier: Apache-2.0
"""Incremental framing and epoch assembly for a mixed GNSS UART stream."""

from collections import Counter
from datetime import date, datetime, time, timezone
import math
import re

from ._checksum import crc24q
from ._types import DCReport, UARTPosition


class UARTDecoder:
    """Return (protocol, raw bytes, identity) after verifying wire checksums."""

    MAX_LINE = 8192
    MAX_BINARY = 4090

    def __init__(self):
        self.pending = bytearray()
        self.errors = Counter()

    def feed(self, data):
        self.pending.extend(data)
        frames = []
        while self.pending:
            first = self.pending[0]
            if first == 0xD3:
                if len(self.pending) < 3:
                    break
                length = ((self.pending[1] & 3) << 8) | self.pending[2]
                if self.pending[1] & 0xFC or length < 2:
                    self._discard("rtcm_header")
                    continue
                size = length + 6
                if len(self.pending) < size:
                    break
                raw = bytes(self.pending[:size])
                if crc24q(raw[:-3]) != int.from_bytes(raw[-3:], "big"):
                    self._discard("rtcm_crc")
                    continue
                frame = ("rtcm", raw, int.from_bytes(raw[3:5], "big") >> 4)
            elif first == 0x7F:
                if len(self.pending) < 5:
                    break
                length = int.from_bytes(self.pending[1:3], "little")
                if (
                    length > self.MAX_BINARY
                    or sum(self.pending[:4]) & 255 != self.pending[4]
                ):
                    self._discard("binary_header")
                    continue
                size = length + 5 + bool(length)
                if len(self.pending) < size:
                    break
                raw = bytes(self.pending[:size])
                if length and sum(raw[5:-1]) & 255 != raw[-1]:
                    self._discard("binary_checksum")
                    continue
                frame = ("binary", raw, raw[3])
            elif 32 <= first < 127:
                end = self.pending.find(b"\n")
                if first not in (ord("$"), ord("[")):
                    markers = [
                        index
                        for marker in (b"$", b"[", b"\xd3", b"\x7f")
                        if (index := self.pending.find(marker)) > 0
                    ]
                    if markers and (end < 0 or min(markers) < end):
                        self._discard("unframed_bytes")
                        continue
                if end < 0:
                    if len(self.pending) <= self.MAX_LINE:
                        break
                    self._discard("long_line")
                    continue
                size = end + 1
                raw = bytes(self.pending[:size])
                if size > self.MAX_LINE:
                    self._discard("long_line")
                    continue
                if first == ord("$"):
                    body, star, checksum = raw.rstrip(b"\r\n")[1:].rpartition(b"*")
                    value = 0
                    for byte in body:
                        value ^= byte
                    try:
                        valid = (
                            star and len(checksum) == 2 and int(checksum, 16) == value
                        )
                    except ValueError:
                        valid = False
                    if not valid or b"," not in body or not body.isascii():
                        self._discard("nmea_checksum")
                        continue
                    frame = ("nmea", raw, body.split(b",", 1)[0].decode("ascii"))
                else:
                    frame = (
                        "text",
                        raw,
                        raw.rstrip(b"\r\n").decode("ascii", errors="replace"),
                    )
            else:
                self._discard("unframed_bytes")
                continue
            frames.append(frame)
            del self.pending[:size]
        return frames

    def _discard(self, name):
        del self.pending[0]
        self.errors[name] += 1


def decode_qzqsm(raw, received_at):
    """Preserve transmitted L1S/CRC bits; only append the four missing pad bits."""
    fields = raw[1 : raw.rindex(b"*")].split(b",")
    if (
        len(fields) != 3
        or fields[0] != b"QZQSM"
        or re.fullmatch(rb"[0-9a-fA-F]{63}", fields[2]) is None
    ):
        raise ValueError("Invalid QZQSM fields")
    satellite_id = int(fields[1])
    if not 1 <= satellite_id <= 255:
        raise ValueError("Invalid QZQSM satellite ID")
    data = bytes.fromhex(fields[2].decode("ascii") + "0")
    return DCReport(satellite_id, data, received_at)


def number(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


class NMEAState:
    """Join GGA/GSA/GSV with RMC within one reported observation epoch."""

    MIN_FIELDS = {"GGA": 12, "GSA": 17, "GSV": 3, "RMC": 9}

    def __init__(self):
        self.gga = None
        self.gsa = []
        self.gsv_pending = {}
        self.gsv_complete = {}
        self.gsv_incomplete = False

    def observe(self, raw):
        from pynmeagps import NMEAReader

        if raw.startswith(b"$P"):
            return None
        message = NMEAReader.parse(raw)
        if message is None or len(message.payload) < self.MIN_FIELDS.get(
            message.msgID, 0
        ):
            return None
        if message.msgID == "GGA":
            self.gga = message
            self.gsa.clear()
            self.gsv_pending.clear()
            self.gsv_complete.clear()
            self.gsv_incomplete = False
        elif message.msgID == "GSA" and self.gga is not None:
            self.gsa.append(message)
        elif message.msgID == "GSV" and self.gga is not None:
            self._satellites(message)
        elif message.msgID == "RMC":
            return self._position(message)
        return None

    def _satellites(self, message):
        fields = vars(message)
        key = message.talker, fields.get("signalID", "")
        if message.msgNum == 1:
            self.gsv_pending[key] = (message.numMsg, 1, set())
        if key not in self.gsv_pending:
            self.gsv_incomplete = True
            return
        total, expected, identifiers = self.gsv_pending[key]
        if message.numMsg != total or message.msgNum != expected:
            del self.gsv_pending[key]
            self.gsv_incomplete = True
            return
        identifiers.update(
            value
            for name, value in fields.items()
            if name.startswith("svid_") and isinstance(value, int) and value > 0
        )
        if message.msgNum == total:
            self.gsv_complete[key] = identifiers
            del self.gsv_pending[key]
        else:
            self.gsv_pending[key] = total, expected + 1, identifiers

    def _position(self, message):
        gga, self.gga = self.gga, None
        gsa, self.gsa = self.gsa, []
        if (
            gga is None
            or not isinstance(message.time, time)
            or gga.time != message.time
        ):
            gga, gsa = None, []
        valid = message.status == "A"
        timestamp = None
        if valid and isinstance(message.date, date) and isinstance(message.time, time):
            timestamp = datetime.combine(
                message.date, message.time, tzinfo=timezone.utc
            ).timestamp()
        modes = [item.navMode for item in gsa if item.navMode in (1, 2, 3)]
        quality = number(gga.quality) if gga is not None else None
        altitude = number(gga.alt) if valid and quality and gga.altUnit == "M" else None
        geoid = number(gga.sep) if gga is not None and gga.sepUnit == "M" else None
        speed = number(message.spd) if valid else None
        total = None
        talkers = {key[0] for key in self.gsv_complete}
        if (
            gga is not None
            and self.gsv_complete
            and not self.gsv_pending
            and not self.gsv_incomplete
            and not ("GN" in talkers and len(talkers) > 1)
        ):
            total = len(
                {
                    (key[0], svid)
                    for key, identifiers in self.gsv_complete.items()
                    for svid in identifiers
                }
            )
        return UARTPosition(
            number(message.lat) if valid else None,
            number(message.lon) if valid else None,
            altitude,
            speed * 1852 / 3600 if speed is not None else None,
            number(message.cog) if valid else None,
            max(modes) if valid and modes else (None if valid else 1),
            int(quality) if quality is not None else None,
            number(gsa[-1].PDOP) if gsa else None,
            number(gga.HDOP) if gga is not None else None,
            number(gsa[-1].VDOP) if gsa else None,
            number(gga.numSV) if gga is not None else None,
            total,
            timestamp,
            geoid,
        )
