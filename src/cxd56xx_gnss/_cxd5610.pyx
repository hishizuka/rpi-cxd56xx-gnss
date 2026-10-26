# SPDX-License-Identifier: Apache-2.0
# cython: language_level=3

# Thin Cython wrapper around the CXD5610 C helper.

from libc.math cimport isnan, isfinite
from libc.errno cimport ETIMEDOUT
from libc.stdint cimport uint8_t, uint64_t
from libc.stddef cimport size_t
from cpython.bytes cimport PyBytes_FromStringAndSize
from threading import Lock
from time import monotonic as _monotonic

from ._types import DCReport, SessionStatus
from ._exceptions import CXD56xxSessionError

# Older Cython on some systems lacks libc.time.timespec, so declare it manually.
cdef extern from "time.h":
    cdef struct timespec:
        long tv_sec
        long tv_nsec


cdef extern from "cxd5610_rpi.h":
  cdef struct cxd5610_ctx
  enum:
    CXD5610_DCREPORT_BYTES
    CXD5610_DCREPORT_QUEUE_SIZE
    CXD5610_SESSION_ERROR
  cdef struct cxd5610_session_status:
    int state
    bint qzss_dcr_requested
    int qzss_dcr_applied
    int error_errno
    int change_errno
    int restore_errno
    int error_stage
  cdef struct cxd5610_dcreport:
    uint8_t satellite_id
    uint8_t data[CXD5610_DCREPORT_BYTES]
    timespec received_at
  cdef struct cxd5610_data:
    double lat
    double lon
    double alt
    double speed
    double track
    int mode
    int status
    double pdop
    double hdop
    double vdop
    int used_sats
    int total_sats
    timespec timestamp

  int cxd5610_create_with_dcr(cxd5610_ctx **out_ctx, bint enabled)
  int cxd5610_set_qzss_dcr_enabled(cxd5610_ctx *ctx, bint enabled) nogil
  int cxd5610_get_session_status(cxd5610_ctx *ctx, cxd5610_session_status *out)
  int cxd5610_wait_ready(cxd5610_ctx *ctx, int timeout_ms) nogil
  int cxd5610_peek(cxd5610_ctx *ctx, cxd5610_data *out)
  int cxd5610_read_dcreports(cxd5610_ctx *ctx, cxd5610_dcreport *out,
                            size_t capacity, uint64_t *dropped)
  int cxd5610_shutdown(cxd5610_ctx *ctx) nogil
  void cxd5610_close(cxd5610_ctx *ctx)


cdef inline object _nan_to_none(double v):
  if isnan(v):
    return None
  return v


cdef inline object _round_or_none(double v, int ndigits):
  if isnan(v):
    return None
  # Use Python's round for consistent decimal rounding.
  return round(v, ndigits)


cdef inline object _ts_to_epoch(timespec ts):
  if ts.tv_sec == 0:
    return None
  return ts.tv_sec + ts.tv_nsec / 1e9


cdef class CXD56xx:
  cdef cxd5610_ctx *ctx
  cdef cxd5610_data data
  cdef object _lifecycle_lock

  cdef _check_result(self, int ret, str message):
    if ret == CXD5610_SESSION_ERROR:
      status = self.session_status
      raise CXD56xxSessionError(
        status.error_errno, message, stage=status.error_stage,
        change_errno=status.change_errno, restore_errno=status.restore_errno)
    if ret < 0:
      raise OSError(-ret, message)

  def __cinit__(self, bint qzss_dcr_enabled=True):
    self.ctx = NULL
    self._lifecycle_lock = Lock()
    cdef int ret = cxd5610_create_with_dcr(&self.ctx, qzss_dcr_enabled)
    if ret != 0:
      # ret is negative errno; convert to positive for OSError
      if ret < 0:
        ret = -ret
      raise OSError(ret, "failed to initialize CXD56xx")

  def __dealloc__(self):
    if self.ctx is not NULL:
      cxd5610_close(self.ctx)
      self.ctx = NULL

  cpdef close(self):
    """Stop GNSS, release I2C/IRQ, and report a missing STOP acknowledgement."""
    cdef cxd5610_ctx *ctx
    cdef int ret
    with self._lifecycle_lock:
      ctx = self.ctx
      if ctx is NULL:
        return
      self.ctx = NULL
      with nogil:
        ret = cxd5610_shutdown(ctx)
    if ret < 0:
      raise OSError(-ret, "failed to stop CXD56xx")

  def set_qzss_dcr_enabled(self, bint enabled):
    """Set receiver L1S reception/DC output; wait for ACKs and return True."""
    cdef int ret
    # Serialize changes and close while the GIL is released for the worker wait.
    with self._lifecycle_lock:
      if self.ctx is NULL:
        raise ValueError("CXD56xx not initialized")
      with nogil:
        ret = cxd5610_set_qzss_dcr_enabled(self.ctx, enabled)
      self._check_result(ret, "failed to configure CXD56xx QZSS DC reports")
    return True

  def wait_ready(self, double timeout=15.0):
    """Wait within one deadline, including lock acquisition; zero only polls."""
    cdef double deadline = _monotonic() + timeout
    if not isfinite(timeout) or timeout < 0 or timeout > 2147483.0:
      raise ValueError("timeout must be finite and between 0 and 2147483 seconds")
    cdef int ret
    cdef int timeout_ms
    cdef double remaining
    if timeout == 0:
      acquired = self._lifecycle_lock.acquire(blocking=False)
    else:
      remaining = max(0.0, deadline - _monotonic())
      acquired = self._lifecycle_lock.acquire(timeout=remaining)
    if not acquired:
      raise TimeoutError(ETIMEDOUT, "CXD56xx readiness lock timed out")
    try:
      if self.ctx is NULL:
        raise ValueError("CXD56xx not initialized")
      remaining = deadline - _monotonic()
      if timeout > 0 and remaining <= 0:
        raise TimeoutError(ETIMEDOUT, "CXD56xx readiness wait timed out")
      # Round down so the C wait cannot deliberately exceed the remaining budget.
      timeout_ms = 0 if timeout == 0 else <int>(remaining * 1000)
      with nogil:
        ret = cxd5610_wait_ready(self.ctx, timeout_ms)
      self._check_result(ret, "CXD56xx session is not ready")
      if timeout > 0 and _monotonic() > deadline:
        raise TimeoutError(ETIMEDOUT, "CXD56xx readiness wait timed out")
    finally:
      self._lifecycle_lock.release()
    return True

  @property
  def session_status(self):
    """Read state and requested/confirmed DCR settings, including after failure."""
    if self.ctx is NULL:
      raise ValueError("CXD56xx not initialized")
    cdef cxd5610_session_status status
    cdef int ret = cxd5610_get_session_status(self.ctx, &status)
    if ret < 0:
      raise OSError(-ret, "failed to read CXD56xx session status")
    return SessionStatus(
      ("starting", "ready", "configuring", "recovering", "failed", "stopped")[status.state],
      bool(status.qzss_dcr_requested),
      None if status.qzss_dcr_applied < 0 else bool(status.qzss_dcr_applied),
      status.error_errno or None, status.change_errno or None,
      status.restore_errno or None,
      (None, "startup", "recovery", "restore", "transport", "worker")[status.error_stage],
    )

  cpdef int peek(self):
    if self.ctx is NULL:
      raise ValueError("CXD56xx not initialized")
    cdef int ret = cxd5610_peek(self.ctx, &self.data)
    if ret == CXD5610_SESSION_ERROR:
      self._check_result(ret, "CXD56xx session failed")
    return ret

  def read_dcreports(self):
    """Drain pending DC reports without waiting for a position fix."""
    if self.ctx is NULL:
      raise ValueError("CXD56xx not initialized")
    cdef cxd5610_dcreport reports[CXD5610_DCREPORT_QUEUE_SIZE]
    cdef int count = cxd5610_read_dcreports(
      self.ctx, reports, CXD5610_DCREPORT_QUEUE_SIZE, NULL)
    self._check_result(count, "failed to read CXD56xx DC reports")
    return [
      DCReport(
        reports[i].satellite_id,
        PyBytes_FromStringAndSize(<const char *>reports[i].data, CXD5610_DCREPORT_BYTES),
        reports[i].received_at.tv_sec + reports[i].received_at.tv_nsec / 1e9,
      )
      for i in range(count)
    ]

  @property
  def dcreports_dropped(self):
    """Cumulative queue-overflow losses since this receiver was created."""
    if self.ctx is NULL:
      raise ValueError("CXD56xx not initialized")
    cdef uint64_t dropped
    cdef int ret = cxd5610_read_dcreports(self.ctx, NULL, 0, &dropped)
    if ret < 0:
      raise OSError(-ret, "failed to read CXD56xx DC report statistics")
    return dropped

  @property
  def lat(self):
    return _nan_to_none(self.data.lat)

  @property
  def lon(self):
    return _nan_to_none(self.data.lon)

  @property
  def alt(self):
    return _nan_to_none(self.data.alt)

  @property
  def speed(self):
    return _round_or_none(self.data.speed, 2)

  @property
  def track(self):
    return _nan_to_none(self.data.track)

  @property
  def mode(self):
    return None if self.data.mode < 0 else self.data.mode

  @property
  def status(self):
    return None if self.data.status < 0 else self.data.status

  @property
  def pdop(self):
    return _round_or_none(self.data.pdop, 1)

  @property
  def hdop(self):
    return _round_or_none(self.data.hdop, 1)

  @property
  def vdop(self):
    return _round_or_none(self.data.vdop, 1)

  @property
  def used_sats(self):
    return None if self.data.used_sats < 0 else self.data.used_sats

  @property
  def total_sats(self):
    return None if self.data.total_sats < 0 else self.data.total_sats

  @property
  def timestamp(self):
    return _ts_to_epoch(self.data.timestamp)
