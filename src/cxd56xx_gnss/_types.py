# SPDX-License-Identifier: Apache-2.0
"""Immutable received-message types shared with the native bindings."""

from typing import NamedTuple

from ._checksum import crc24q


class DCReport(NamedTuple):
    """One L1S message: QZQSM satellite ID, 32 raw bytes, host Unix receive time."""

    satellite_id: int
    data: bytes
    received_at: float

    def to_azarashi_packet(self, *, reconstruct_crc: bool = False) -> bytes:
        """Return satellite ID + 32 bytes for azarashi's ``net`` input.

        Explicit reconstruction replaces CRC bits 226..249 in a copy with
        CRC24Q of the first 226 bits. This enables parsing, not validation
        of the received message. The original data and timestamp are retained.
        """
        if type(reconstruct_crc) is not bool:
            raise TypeError("reconstruct_crc must be bool")
        if not isinstance(self.data, bytes) or len(self.data) != 32:
            raise ValueError("data must be exactly 32 bytes")
        if type(self.satellite_id) is not int or not 1 <= self.satellite_id <= 255:
            raise ValueError("satellite_id must be an integer from 1 to 255")
        data = self.data
        if reconstruct_crc:
            crc = crc24q(data, bit_length=226)
            # Retain all protected bits and the six padding bits.
            value = int.from_bytes(data, "big")
            value = (value & ~(0xFFFFFF << 6)) | ((crc & 0xFFFFFF) << 6)
            data = value.to_bytes(32, "big")
        return bytes((self.satellite_id,)) + data


class SessionStatus(NamedTuple):
    """Receiver state and confirmed settings; errno fields are positive or None."""

    state: str
    qzss_dcr_requested: bool
    qzss_dcr_applied: bool | None
    error_errno: int | None
    change_errno: int | None
    restore_errno: int | None
    error_stage: str | None


class UARTFrame(NamedTuple):
    """One UART frame with host Unix time; text has no wire checksum."""

    protocol: str
    message_type: str | int
    data: bytes
    received_at: float


class RTCMFrame(NamedTuple):
    """A complete CRC24Q-verified RTCM3 frame; measurement validity is separate."""

    message_type: int
    data: bytes
    received_at: float


class UARTPosition(NamedTuple):
    """One RMC epoch; alt is GGA height above mean sea level in metres."""

    lat: float | None = None
    lon: float | None = None
    alt: float | None = None
    speed: float | None = None
    track: float | None = None
    mode: int | None = None
    status: int | None = None
    pdop: float | None = None
    hdop: float | None = None
    vdop: float | None = None
    used_sats: int | None = None
    total_sats: int | None = None
    timestamp: float | None = None
    geoid_separation: float | None = None


class UARTSessionStatus(NamedTuple):
    """UART lifecycle and acknowledged volatile settings; None means unknown."""

    state: str
    rtcm_mask: int | None
    satellite_mask: int | None
    error_errno: int | None
    error_stage: str | None
    qzss_dcr_requested: bool | None
    qzss_dcr_applied: bool | None
