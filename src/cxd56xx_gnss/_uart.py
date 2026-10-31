# SPDX-License-Identifier: Apache-2.0
"""POSIX UART receiver with independent position, RTCM and raw frame queues."""

from collections import Counter, deque
import errno
import fcntl
import math
import os
from operator import attrgetter
import re
import select
import termios
import threading
import time

from pynmeagps import NMEAParseError, NMEATypeError

from ._exceptions import CXD56xxSessionError, UARTCommandError
from ._types import RTCMFrame, UARTFrame, UARTPosition, UARTSessionStatus
from ._uart_protocol import NMEAState, UARTDecoder, decode_qzqsm


class CXD56xxUART:
    """Adopt a UART without resetting the receiver or changing its firmware."""

    WAKE_TIMEOUT = 2.5
    WAKE_ATTEMPTS = 10
    _DCR_NMEA_MASK = 0x0E0F7FEF
    _DEFAULT_NMEA_MASK = 0xEF

    def __init__(
        self,
        device,
        *,
        baudrate=115200,
        rtscts=False,
        command_timeout=5.0,
        queue_size=256,
        qzss_dcr_enabled=None,
    ):
        if type(baudrate) is not int or baudrate <= 0:
            raise ValueError("baudrate must be a positive integer")
        speed = getattr(termios, f"B{baudrate}", None)
        if speed is None:
            raise ValueError(f"Host does not support {baudrate} baud")
        if type(rtscts) is not bool or type(queue_size) is not int or queue_size <= 0:
            raise ValueError("rtscts must be bool and queue_size a positive integer")
        if not math.isfinite(command_timeout) or command_timeout <= 0:
            raise ValueError("command_timeout must be positive and finite")
        if qzss_dcr_enabled is not None and type(qzss_dcr_enabled) is not bool:
            raise TypeError("qzss_dcr_enabled must be bool or None")
        self._timeout = command_timeout
        self._condition = threading.Condition()
        self._commands = threading.Lock()
        self._stop = threading.Event()
        self._error = None
        self._state = "ready"
        self._pending = None
        self._decoder = UARTDecoder()
        self._nmea = NMEAState()
        self._latest = self._data = UARTPosition()
        self._version = 0
        self._peek_version = 0
        self._queues = {
            name: deque(maxlen=queue_size) for name in ("frames", "rtcm", "dcreports")
        }
        self._dropped = Counter()
        self._parse_errors = Counter()
        self._rtcm_mask = None
        self._satellite_mask = None
        self._qzss_dcr_requested = None
        self._qzss_dcr_applied = None
        fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            # This lock coordinates library users; external HAL ownership is separate.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            settings = termios.tcgetattr(fd)
            settings[:6] = [
                0,
                0,
                termios.CLOCAL
                | termios.CREAD
                | termios.CS8
                | (termios.CRTSCTS if rtscts else 0),
                0,
                speed,
                speed,
            ]
            settings[6][termios.VMIN] = 0
            settings[6][termios.VTIME] = 0
            # No HUPCL, GPIO, DTR/RTS ioctl, input flush or receiver baud command.
            termios.tcsetattr(fd, termios.TCSANOW, settings)
            self._fd = fd
            self._worker = threading.Thread(
                target=self._read, name="cxd56xx-uart", daemon=True
            )
            self._worker.start()
        except BaseException:
            os.close(fd)
            raise

        if qzss_dcr_enabled is not None:
            try:
                self.set_qzss_dcr_enabled(qzss_dcr_enabled)
            except BaseException:
                self.close()
                raise

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        self.close()

    def _check(self):
        if self._state == "closed":
            raise ValueError("CXD56xx UART is closed")
        if self._error is not None:
            code, stage, message = self._error
            raise CXD56xxSessionError(code, message, stage=stage)

    def _fail(self, code, stage, message):
        with self._condition:
            if self._error is None:
                self._error = code, stage, message
                self._state = "failed"
                self._qzss_dcr_applied = None
            self._stop.set()
            self._condition.notify_all()

    def _append(self, name, value):
        queue = self._queues[name]
        if len(queue) == queue.maxlen:
            self._dropped[name] += 1
        queue.append(value)

    def _read(self):
        try:
            while not self._stop.is_set():
                readable, _, _ = select.select([self._fd], [], [], 0.1)
                if not readable:
                    continue
                try:
                    data = os.read(self._fd, 4096)
                except BlockingIOError:
                    continue
                if not data:
                    raise OSError(errno.EIO, "GNSS UART closed")
                received_at = time.time()
                with self._condition:
                    for protocol, raw, identity in self._decoder.feed(data):
                        self._append(
                            "frames", UARTFrame(protocol, identity, raw, received_at)
                        )
                        if protocol == "text":
                            self._reply(identity)
                        elif protocol == "rtcm":
                            self._append("rtcm", RTCMFrame(identity, raw, received_at))
                        elif protocol == "nmea":
                            try:
                                if identity == "QZQSM":
                                    # Keep passive reception, but discard disabled/in-flight reports.
                                    if self._state == "ready" and (
                                        self._qzss_dcr_requested is None
                                        or self._qzss_dcr_applied is True
                                    ):
                                        self._append(
                                            "dcreports", decode_qzqsm(raw, received_at)
                                        )
                                else:
                                    position = self._nmea.observe(raw)
                                    if position is not None:
                                        self._latest = position
                                        self._version += 1
                            except (
                                NMEAParseError,
                                NMEATypeError,
                                ValueError,
                                OverflowError,
                            ):
                                self._parse_errors[identity] += 1
                    self._condition.notify_all()
        except OSError as exc:
            self._fail(exc.errno or errno.EIO, "transport", str(exc))
        except Exception as exc:
            self._fail(errno.EIO, "reader", str(exc))
        finally:
            with self._condition:
                fd, self._fd = self._fd, None
                os.close(fd)
                self._condition.notify_all()

    def _reply(self, line):
        pending = self._pending
        if pending is None:
            return
        prefix = f"[{pending['name']}]"
        if line == prefix + " Done":
            pending["done"] = True
        elif line.startswith(prefix + " Err"):
            pending["error"] = line
            pending["done"] = True
        else:
            pending["body"].append(line)

    def _write(self, data, deadline):
        pending = memoryview(data)
        while pending:
            if time.monotonic() >= deadline:
                raise TimeoutError(errno.ETIMEDOUT, "UART write timed out")
            with self._condition:
                self._check()
                try:
                    written = os.write(self._fd, pending)
                except BlockingIOError:
                    written = 0
                fd = self._fd
            if written:
                pending = pending[written:]
            else:
                select.select(
                    [], [fd], [], min(0.1, max(0, deadline - time.monotonic()))
                )

    def _request(self, name, argument=None, *, timeout=None, fatal_timeout=True):
        timeout = self._timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        pending = {"name": name, "done": False, "error": None, "body": []}
        with self._condition:
            self._check()
            self._pending = pending
        command = f"@{name}" + (f" {argument}" if argument is not None else "")
        try:
            self._write((command + "\r\n").encode("ascii"), deadline)
            with self._condition:
                while not pending["done"]:
                    self._check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(errno.ETIMEDOUT, f"No {name} reply")
                    self._condition.wait(remaining)
                self._check()
                if pending["error"] is not None:
                    raise UARTCommandError(name, pending["error"])
                return pending["body"]
        except TimeoutError:
            if fatal_timeout:
                self._fail(
                    errno.ETIMEDOUT,
                    f"command:{name}",
                    f"No acknowledged {name} completion",
                )
                self._check()
            raise
        except OSError as exc:
            if isinstance(exc, (UARTCommandError, CXD56xxSessionError)):
                raise
            self._fail(exc.errno or errno.EIO, "write", str(exc))
            self._check()
        finally:
            with self._condition:
                self._pending = None

    def _wake(self):
        for attempt in range(self.WAKE_ATTEMPTS):
            try:
                self._request("WUP", timeout=self.WAKE_TIMEOUT, fatal_timeout=False)
                return
            except TimeoutError:
                if attempt + 1 == self.WAKE_ATTEMPTS:
                    self._fail(errno.ETIMEDOUT, "wake", "No wake acknowledgement")
                    self._check()

    def _idle(self):
        self._wake()
        self._request("GSTP")
        with self._condition:
            self._nmea = NMEAState()
            self._latest = self._data = UARTPosition()
            self._peek_version = self._version

    def start(self):
        """Wake, stop and hot-start positioning; never reset or load firmware."""
        with self._commands:
            self._idle()
            self._request("GSR")
        return True

    def stop(self):
        with self._commands:
            self._idle()
        return True

    def set_rtcm_mask(self, mask):
        """Set the volatile ASCII RTCM output mask and restart positioning."""
        if type(mask) is not int or not 0 <= mask < 1 << 27:
            raise ValueError("RTCM mask must be an integer from 0 through 0x7ffffff")
        with self._commands:
            self._idle()
            self._request("RTCM", hex(mask))
            with self._condition:
                self._rtcm_mask = mask
            self._request("GSR")
        return True

    def _get_satellite_mask(self):
        lines = self._request("GGNS")
        masks = [
            int(line, 16) for line in lines if re.fullmatch(r"0x[0-9a-fA-F]+", line)
        ]
        if len(masks) != 1:
            self._fail(errno.EPROTO, "GGNS", "No unambiguous satellite mask")
            self._check()
        with self._condition:
            self._satellite_mask = masks[0]
        return masks[0]

    def get_satellite_mask(self):
        with self._commands:
            self._wake()
            return self._get_satellite_mask()

    def _set_l1s_mask(self, enabled):
        original = self._get_satellite_mask()
        requested = original | 0x20 if enabled else original & ~0x20
        self._request("GNS", hex(requested))
        if self._get_satellite_mask() != requested:
            self._fail(errno.EPROTO, "GNS", "Satellite mask readback mismatch")
            self._check()

    def set_l1s_enabled(self, enabled):
        """Change tracking only; invalidate any previously managed DCR setting."""
        if type(enabled) is not bool:
            raise TypeError("enabled must be bool")
        with self._commands:
            self._idle()
            with self._condition:
                if self._qzss_dcr_requested is not None:
                    self._qzss_dcr_applied = None
                    self._queues["dcreports"].clear()
            self._set_l1s_mask(enabled)
            self._request("GSR")
        return True

    def set_qzss_dcr_enabled(self, enabled):
        """Set L1S and volatile QZQSM output, then hot-start after all ACKs.

        The enabled profile is a verified, verbose v00.201 sentence mask.
        Disabling selects the documented default, not the unknown prior mask.
        A partial change fails the session because BSSL has no readback here.
        """
        if type(enabled) is not bool:
            raise TypeError("enabled must be bool")
        with self._commands:
            with self._condition:
                self._check()
                self._state = "configuring"
                self._qzss_dcr_requested = enabled
                self._qzss_dcr_applied = None
                self._queues["dcreports"].clear()
            try:
                self._idle()
                self._set_l1s_mask(enabled)
                mask = self._DCR_NMEA_MASK if enabled else self._DEFAULT_NMEA_MASK
                self._request("BSSL", hex(mask))
                self._request("GSR")
            except UARTCommandError as exc:
                self._fail(exc.errno, "qzss_dcr", exc.response)
                self._check()
            with self._condition:
                self._check()
                self._queues["dcreports"].clear()
                self._qzss_dcr_applied = enabled
                self._state = "ready"
        return True

    def peek(self):
        with self._condition:
            self._check()
            if self._version == self._peek_version:
                return -errno.EAGAIN
            self._data = self._latest
            self._peek_version = self._version
            return 0

    def _drain(self, name):
        with self._condition:
            self._check()
            result = list(self._queues[name])
            self._queues[name].clear()
            return result

    def read_rtcm(self):
        return self._drain("rtcm")

    def read_dcreports(self):
        """Drain DCReport objects independently of position availability."""
        return self._drain("dcreports")

    def read_frames(self):
        return self._drain("frames")

    @property
    def position(self):
        return self._data

    @property
    def session_status(self):
        with self._condition:
            return UARTSessionStatus(
                self._state,
                self._rtcm_mask,
                self._satellite_mask,
                self._error[0] if self._error else None,
                self._error[1] if self._error else None,
                self._qzss_dcr_requested,
                self._qzss_dcr_applied,
            )

    @property
    def dropped(self):
        with self._condition:
            return {name: self._dropped[name] for name in self._queues}

    @property
    def dcreports_dropped(self):
        """Cumulative report queue-overflow losses, matching the I2C API."""
        with self._condition:
            return self._dropped["dcreports"]

    @property
    def parse_errors(self):
        with self._condition:
            return dict(self._decoder.errors + self._parse_errors)

    def close(self):
        """Join the reader and release the UART; no receiver command is sent."""
        with self._commands:
            if self._state == "closed":
                return
            self._stop.set()
            self._worker.join()
            with self._condition:
                self._state = "closed"

    lat = property(attrgetter("_data.lat"))
    lon = property(attrgetter("_data.lon"))
    alt = property(attrgetter("_data.alt"))
    speed = property(attrgetter("_data.speed"))
    track = property(attrgetter("_data.track"))
    mode = property(attrgetter("_data.mode"))
    status = property(attrgetter("_data.status"))
    pdop = property(attrgetter("_data.pdop"))
    hdop = property(attrgetter("_data.hdop"))
    vdop = property(attrgetter("_data.vdop"))
    used_sats = property(attrgetter("_data.used_sats"))
    total_sats = property(attrgetter("_data.total_sats"))
    timestamp = property(attrgetter("_data.timestamp"))
    geoid_separation = property(attrgetter("_data.geoid_separation"))
