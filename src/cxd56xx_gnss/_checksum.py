# SPDX-License-Identifier: Apache-2.0
"""CRC24Q for complete frames and bit-aligned GNSS payloads."""


def crc24q(data, *, bit_length=None):
    """Calculate CRC24Q over the leading bits, or all bytes by default."""
    size, remainder = divmod(len(data) * 8 if bit_length is None else bit_length, 8)
    crc = 0
    for byte in data[:size]:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    if remainder:
        crc ^= (data[size] >> (8 - remainder)) << (24 - remainder)
        for _ in range(remainder):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc
