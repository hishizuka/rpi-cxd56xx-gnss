# SPDX-License-Identifier: Apache-2.0
"""Receiver session failures and command rejections."""

import errno


class UARTCommandError(OSError):
    """A definite receiver rejection; response retains the original reply."""

    def __init__(self, command, response):
        super().__init__(errno.EPROTO, response)
        self.command = command
        self.response = response


class CXD56xxSessionError(OSError):
    """A terminal session failure; subsequent reads cannot resume this session."""

    def __init__(self, errno, message, *, stage, change_errno=None, restore_errno=None):
        super().__init__(errno, message)
        self.stage = stage
        self.change_errno = change_errno
        self.restore_errno = restore_errno
