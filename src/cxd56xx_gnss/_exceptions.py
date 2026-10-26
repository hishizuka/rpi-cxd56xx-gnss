# SPDX-License-Identifier: Apache-2.0
"""Errors that require closing and recreating a receiver session."""


class CXD56xxSessionError(OSError):
    """A terminal session failure; subsequent reads cannot resume this session."""

    def __init__(self, errno, message, *, stage, change_errno=None, restore_errno=None):
        super().__init__(errno, message)
        self.stage = stage
        self.change_errno = change_errno
        self.restore_errno = restore_errno
