# SPDX-License-Identifier: Apache-2.0
"""Immutable received-message types shared with the native bindings."""

from typing import NamedTuple


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
            crc = 0
            for bit in range(226):
                crc = (crc << 1) ^ (((data[bit // 8] >> (7 - bit % 8)) & 1) << 24)
                if crc & 0x1000000:
                    crc ^= 0x1864CFB
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
