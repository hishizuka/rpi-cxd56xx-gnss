# SPDX-License-Identifier: Apache-2.0
"""Sony CXD56xx GNSS bindings; the current implementation targets CXD5610."""

from ._exceptions import CXD56xxSessionError, UARTCommandError
from ._types import (
    DCReport,
    RTCMFrame,
    SessionStatus,
    UARTFrame,
    UARTPosition,
    UARTSessionStatus,
)

__all__ = [
    "CXD56xx",
    "CXD56xxUART",
    "CXD56xxSessionError",
    "UARTCommandError",
    "DCReport",
    "SessionStatus",
    "RTCMFrame",
    "UARTFrame",
    "UARTPosition",
    "UARTSessionStatus",
]


def __getattr__(name):
    # UART-only installations must not import the Linux I2C extension.
    if name == "CXD56xx":
        from ._cxd5610 import CXD56xx as driver
    elif name == "CXD56xxUART":
        from ._uart import CXD56xxUART as driver
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = driver
    return driver
