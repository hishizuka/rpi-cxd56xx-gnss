# SPDX-License-Identifier: Apache-2.0
"""Test the real Cython API and C worker with a hardware-free wire fixture."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = """
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ctypes
import errno
import importlib
import time
import threading
from cxd56xx_gnss import CXD56xx, CXD56xxSessionError, DCReport, SessionStatus

native = importlib.import_module('cxd56xx_gnss._cxd5610')
wire = ctypes.CDLL(native.__file__)
wire.cxd5610_test_mode.argtypes = [ctypes.c_int]
wire.cxd5610_test_mode.restype = None
for name in ('report', 'corrupt', 'io_failure'):
    fn = getattr(wire, 'cxd5610_test_' + name)
    fn.argtypes = []
    fn.restype = None
for name in ('starts', 'mask'):
    fn = getattr(wire, 'cxd5610_test_' + name)
    fn.argtypes = []
    fn.restype = ctypes.c_int

def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.002)
    raise AssertionError('condition did not become true')

@contextmanager
def raises(kind):
    caught = []
    try:
        yield caught
    except kind as exc:
        caught.append(exc)
    else:
        raise AssertionError('expected ' + kind.__name__)
"""


class NativeAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("Cython") is None:
            raise unittest.SkipTest("Native API tests require Cython and setuptools")
        include = os.environ.get("CXD56XX_TEST_INCLUDE")
        if sys.platform != "linux" and not include:
            raise unittest.SkipTest("Linux/libgpiod required; macOS can provide CXD56XX_TEST_INCLUDE")
        cls.directory = tempfile.TemporaryDirectory(prefix="cxd56xx-native-api-")
        cls.addClassCleanup(cls.directory.cleanup)
        workspace = Path(cls.directory.name)
        package = workspace / "src" / "cxd56xx_gnss"
        package.mkdir(parents=True)
        for name in ("_cxd5610.pyx", "_types.py", "_exceptions.py", "__init__.py"):
            shutil.copyfile(ROOT / "src" / "cxd56xx_gnss" / name, package / name)
        cls.library = workspace / "lib"
        (cls.library / "cxd56xx_gnss").mkdir(parents=True)
        for name in ("_types.py", "_exceptions.py", "__init__.py"):
            shutil.copyfile(package / name, cls.library / "cxd56xx_gnss" / name)
        includes = [str(ROOT / "src" / "cxd56xx_gnss")]
        if include:
            includes.append(include)
        mac = sys.platform == "darwin"
        script = f"""
from setuptools import setup, Extension
from Cython.Build import cythonize
setup(ext_modules=cythonize([Extension(
    'cxd56xx_gnss._cxd5610',
    sources={[str(package / '_cxd5610.pyx'), str(ROOT / 'tests' / 'native_receiver.c')]!r},
    include_dirs={includes!r},
    undef_macros=['NDEBUG'],
    libraries={[] if mac else ['gpiod', 'pthread', 'm']!r},
    extra_compile_args={['-pthread', '-std=gnu11'] + (['-fvisibility=hidden'] if mac else [])!r},
    extra_link_args={['-Wl,-dead_strip', '-Wl,-undefined,dynamic_lookup'] if mac else []!r},
)], build_dir={str(workspace / 'cython')!r}, compiler_directives={{'language_level': 3}}))
"""
        result = subprocess.run(
            [sys.executable, "-c", script, "build_ext", "--build-lib", str(cls.library),
             "--build-temp", str(workspace / "objects")],
            cwd=workspace, capture_output=True, text=True, timeout=180,
        )
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)

    def run_api(self, body, mode=0):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.library)
        env["CXD56XX_TEST_MODE"] = str(mode)
        result = subprocess.run(
            [sys.executable, "-c", PRELUDE + textwrap.dedent(body)],
            cwd=self.directory.name, env=env, capture_output=True, text=True, timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_confirmed_startup_and_healthy_empty_reads(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.run_api(f"""
                gps = CXD56xx(qzss_dcr_enabled={enabled})
                assert gps.wait_ready() is True
                status = gps.session_status
                assert isinstance(status, SessionStatus)
                assert status.state == 'ready'
                assert status.qzss_dcr_requested is {enabled}
                assert status.qzss_dcr_applied is {enabled}
                assert gps.peek() == -errno.EAGAIN
                assert gps.read_dcreports() == []
                assert gps.dcreports_dropped == 0
                assert wire.cxd5610_test_mask() == {0xef if enabled else 0xcf}
                gps.close()
                """)

    def test_startup_missing_satellite_ack(self):
        self.run_api("""
            gps = CXD56xx()
            assert gps.session_status.qzss_dcr_applied is None
            with raises(TimeoutError):
                gps.wait_ready(0.005)
            with raises(CXD56xxSessionError) as error:
                gps.wait_ready(1)
            assert error[0].errno == errno.ETIMEDOUT
            assert error[0].stage == 'startup'
            assert gps.session_status.state == 'failed'
            assert gps.session_status.qzss_dcr_applied is None
            assert gps.dcreports_dropped == 0
            gps.close()
        """, mode=3)

    def test_startup_missing_output_or_start_ack(self):
        for mode in (4, 5):
            with self.subTest(mode=mode):
                self.run_api("""
                    gps = CXD56xx(qzss_dcr_enabled=False)
                    with raises(CXD56xxSessionError) as error:
                        gps.wait_ready(1)
                    assert error[0].stage == 'startup'
                    assert error[0].errno == errno.ETIMEDOUT
                    assert gps.session_status.qzss_dcr_applied is None
                    for read in (gps.peek, gps.read_dcreports):
                        with raises(CXD56xxSessionError): read()
                    gps.close()
                """, mode=mode)

    def test_change_failure_with_successful_restoration(self):
        self.run_api("""
            gps = CXD56xx()
            gps.wait_ready()
            wire.cxd5610_test_mode(1)
            with raises(OSError) as error:
                gps.set_qzss_dcr_enabled(False)
            assert type(error[0]) is OSError and error[0].errno == errno.EINVAL
            status = gps.session_status
            assert status.state == 'ready' and status.qzss_dcr_applied is True
            assert status.change_errno == errno.EINVAL and status.restore_errno is None
            assert gps.wait_ready(0) is True
            assert gps.peek() == -errno.EAGAIN and gps.read_dcreports() == []
            wire.cxd5610_test_mode(0)
            assert gps.set_qzss_dcr_enabled(False) is True
            assert gps.session_status.change_errno is None
            gps.close()
        """)

    def test_failed_restoration_is_sticky_and_can_be_recreated(self):
        for mode, terminal_errno in ((2, "EIO"), (7, "EINVAL")):
            with self.subTest(mode=mode):
                self.check_failed_restoration(mode, terminal_errno)

    def check_failed_restoration(self, mode, terminal_errno):
        self.run_api("""
            gps = CXD56xx()
            gps.wait_ready()
            wire.cxd5610_test_report()
            time.sleep(0.02)
            wire.cxd5610_test_mode(MODE)
            operations = [lambda: gps.set_qzss_dcr_enabled(False), gps.peek,
                          gps.read_dcreports, gps.wait_ready]
            for operation in operations:
                with raises(CXD56xxSessionError) as error: operation()
                assert isinstance(error[0], OSError)
                assert error[0].stage == 'restore' and error[0].errno == errno.TERMINAL
                assert error[0].change_errno == errno.EINVAL
                assert error[0].restore_errno == errno.TERMINAL
            assert gps.session_status.state == 'failed'
            assert gps.session_status.qzss_dcr_applied is None
            gps.close()
            other = CXD56xx(qzss_dcr_enabled=False)
            other.wait_ready()
            assert other.session_status.qzss_dcr_applied is False
            other.close()
        """.replace("MODE", str(mode)).replace("TERMINAL", terminal_errno))

    def test_worker_transport_failure_and_close_error_release_resources(self):
        self.run_api("""
            gps = CXD56xx()
            gps.wait_ready()
            wire.cxd5610_test_io_failure()
            until(lambda: gps.session_status.state == 'failed')
            for operation in (gps.peek, gps.read_dcreports, gps.wait_ready):
                with raises(CXD56xxSessionError) as error: operation()
                assert error[0].stage == 'transport' and error[0].errno == errno.EIO
            with raises(OSError): gps.close()
            with raises(ValueError): gps.read_dcreports()
            other = CXD56xx()
            other.wait_ready()
            other.close()
        """)

    def test_recovery_requires_both_acks(self):
        for mode in (3, 4, 5):
            with self.subTest(mode=mode):
                self.run_api("""
                    gps = CXD56xx(qzss_dcr_enabled=False)
                    gps.wait_ready()
                    wire.cxd5610_test_mode(MODE)
                    wire.cxd5610_test_corrupt()
                    until(lambda: gps.session_status.state == 'failed')
                    for operation in (gps.wait_ready, gps.peek, gps.read_dcreports):
                        with raises(CXD56xxSessionError) as error: operation()
                        assert error[0].stage == 'recovery'
                        assert error[0].errno == errno.ETIMEDOUT
                    gps.close()
                """.replace("MODE", str(mode)))

    def test_recovery_retains_last_confirmed_setting(self):
        self.run_api("""
            gps = CXD56xx()
            gps.wait_ready()
            gps.set_qzss_dcr_enabled(False)
            wire.cxd5610_test_corrupt()
            until(lambda: wire.cxd5610_test_starts() == 2)
            assert gps.wait_ready() is True
            assert gps.session_status.state == 'ready'
            assert gps.session_status.qzss_dcr_applied is False
            assert wire.cxd5610_test_mask() == 0xcf
            gps.close()
        """)

    def test_raw_reports_without_fix_and_off_does_not_replay(self):
        self.run_api("""
            gps = CXD56xx()
            gps.wait_ready()
            wire.cxd5610_test_report()
            reports = until(gps.read_dcreports)
            assert len(reports) == 1 and isinstance(reports[0], DCReport)
            assert reports[0].satellite_id == 55
            assert reports[0].data == bytes(range(32))
            assert reports[0].received_at > 0 and gps.peek() == -errno.EAGAIN
            gps.set_qzss_dcr_enabled(False)
            wire.cxd5610_test_report()
            time.sleep(0.03)
            assert gps.read_dcreports() == []
            gps.set_qzss_dcr_enabled(True)
            assert gps.read_dcreports() == []
            gps.close()
        """)

    def test_synthetic_notification_payload_reaches_python_unchanged(self):
        path = ROOT / "tests" / "data" / "dcreport-synthetic.json"
        payloads = json.loads(path.read_text())["notification_payload_hex"]
        self.run_api(f"""
            gps = CXD56xx()
            gps.wait_ready()
            wire.cxd5610_test_payload.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
            wire.cxd5610_test_payload.restype = None
            for value in {payloads!r}:
                payload = bytes.fromhex(value)
                wire.cxd5610_test_payload(payload, len(payload))
                report = until(gps.read_dcreports)[0]
                assert report.satellite_id == payload[2]
                assert report.data == payload[3:]
                assert report.to_azarashi_packet() == payload[2:]
                packet = report.to_azarashi_packet(reconstruct_crc=True)
                assert len(packet) == 33 and packet[0] == report.satellite_id
                assert int.from_bytes(packet[1:], 'big') >> 30 == int.from_bytes(report.data, 'big') >> 30
                assert packet[-1] & 63 == report.data[-1] & 63
                assert report.data == payload[3:]
            gps.close()
        """)

    def test_close_waits_for_in_flight_setting(self):
        self.run_api("""
            gps = CXD56xx()
            gps.wait_ready()
            wire.cxd5610_test_mode(6)
            with ThreadPoolExecutor(2) as pool:
                setting = pool.submit(gps.set_qzss_dcr_enabled, False)
                until(lambda: gps.session_status.state == 'configuring')
                closing = pool.submit(gps.close)
                time.sleep(0.02)
                assert not closing.done()
                assert setting.result(timeout=2) is True
                closing.result(timeout=2)
            gps.close()
            other = CXD56xx()
            other.wait_ready()
            other.close()
        """)

    def test_cancelled_to_thread_is_serialized_with_close(self):
        self.run_api("""
            async def check():
                gps = CXD56xx()
                await asyncio.to_thread(gps.wait_ready)
                wire.cxd5610_test_mode(6)
                change = asyncio.create_task(asyncio.to_thread(gps.set_qzss_dcr_enabled, False))
                while gps.session_status.state != 'configuring':
                    await asyncio.sleep(0.002)
                change.cancel()
                with raises(asyncio.CancelledError): await change
                await asyncio.wait_for(asyncio.to_thread(gps.close), 2)
                other = CXD56xx()
                await asyncio.to_thread(other.wait_ready)
                other.close()
            asyncio.run(check())
        """)

    def test_concurrent_changes_are_serialized(self):
        self.run_api("""
            gps = CXD56xx()
            gps.wait_ready()
            wire.cxd5610_test_mode(6)
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(gps.set_qzss_dcr_enabled, False)
                until(lambda: gps.session_status.state == 'configuring')
                second = pool.submit(gps.set_qzss_dcr_enabled, True)
                assert first.result(timeout=2) is True and second.result(timeout=2) is True
            assert gps.session_status.qzss_dcr_applied is True
            gps.close()
        """)

    def test_wait_ready_timeout_includes_contended_lock(self):
        self.run_api("""
            gps = CXD56xx()
            gps.wait_ready()
            for timeout in (0, 0.005, 0.03):
                wire.cxd5610_test_mode(6)
                with ThreadPoolExecutor(1) as pool:
                    setting = pool.submit(gps.set_qzss_dcr_enabled, False)
                    until(lambda: gps.session_status.state == 'configuring')
                    started = time.monotonic()
                    with raises(TimeoutError) as error: gps.wait_ready(timeout)
                    elapsed = time.monotonic() - started
                    assert error[0].errno == errno.ETIMEDOUT
                    assert elapsed < 0.1, elapsed
                    assert setting.result(timeout=2) is True
                assert gps.session_status.state == 'ready'
                assert gps.session_status.qzss_dcr_applied is False
            gps.close()
        """)

    def test_wait_ready_shares_budget_between_lock_and_c_wait(self):
        self.run_api("""
            gps = CXD56xx()
            entered = threading.Event()
            def first_wait():
                entered.set()
                return gps.wait_ready(0.12)
            with ThreadPoolExecutor(1) as pool:
                waiting = pool.submit(first_wait)
                assert entered.wait(1)
                time.sleep(0.02)
                started = time.monotonic()
                with raises(TimeoutError): gps.wait_ready(0.16)
                elapsed = time.monotonic() - started
                assert 0.12 < elapsed < 0.23, elapsed
                with raises(TimeoutError): waiting.result(timeout=1)
            assert gps.session_status.state == 'starting'
            with raises(CXD56xxSessionError): gps.wait_ready(1)
            gps.close()
        """, mode=3)

    def test_wait_ready_succeeds_within_deadline_after_lock_contention(self):
        self.run_api("""
            gps = CXD56xx()
            gps.wait_ready()
            wire.cxd5610_test_mode(6)
            with ThreadPoolExecutor(1) as pool:
                setting = pool.submit(gps.set_qzss_dcr_enabled, False)
                until(lambda: gps.session_status.state == 'configuring')
                started = time.monotonic()
                assert gps.wait_ready(0.5) is True
                elapsed = time.monotonic() - started
                assert 0.05 < elapsed < 0.45, elapsed
                assert setting.result(timeout=1) is True
            assert gps.session_status.qzss_dcr_applied is False
            gps.close()
        """)

    def test_wait_ready_zero_is_nonblocking_and_preserves_session_errors(self):
        self.run_api("""
            gps = CXD56xx()
            started = time.monotonic()
            with raises(TimeoutError): gps.wait_ready(0)
            assert time.monotonic() - started < 0.1
            with raises(CXD56xxSessionError): gps.wait_ready(1)
            with raises(CXD56xxSessionError): gps.wait_ready(0)
            gps.close()
        """, mode=3)
        self.run_api("""
            gps = CXD56xx()
            gps.wait_ready()
            started = time.monotonic()
            assert gps.wait_ready(0) is True
            assert time.monotonic() - started < 0.1
            gps.close()
        """)

    def test_closed_apis_and_invalid_wait_timeouts(self):
        self.run_api("""
            gps = CXD56xx()
            gps.wait_ready()
            for timeout in (-1, float('inf'), float('nan'), 2147484):
                with raises(ValueError): gps.wait_ready(timeout)
            gps.close()
            gps.close()
            for operation in (gps.wait_ready, gps.peek, gps.read_dcreports,
                              lambda: gps.set_qzss_dcr_enabled(True),
                              lambda: gps.session_status, lambda: gps.dcreports_dropped):
                with raises(ValueError): operation()
        """)
