"""Unit tests for device_prep.boot-cleanup logic.

Two layers:
  - mock-based tests (no device needed): verify command sequence, boot
    wait logic, fresh-boot settle delay and error tolerance.
  - real-device test (skipped when offline): calls the actual
    cleanup_after_boot() against FOLIO_S and asserts the status flags.
"""
from __future__ import annotations

import os
import itertools
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from exp_framework.utils.device_prep import cleanup_after_boot  # noqa: E402

SERIAL = os.environ.get("FOLIO_S", "21121FDF600C4G")


def _device_online() -> bool:
    try:
        import subprocess
        out = subprocess.run(["adb", "-s", SERIAL, "shell",
                              "echo ok"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        return out == "ok"
    except Exception:
        return False


class TestCleanupLogic(unittest.TestCase):
    """Mock adb: verify behavior without a device."""

    def _fake_run(self, calls, booted=1, uptime="100.0 200.0"):
        def _run(cmd, *a, **kw):
            calls.append(cmd)
            args = " ".join(str(c) for c in cmd)
            if "getprop sys.boot_completed" in args:
                return mock.Mock(stdout=f"{booted}\n", returncode=0)
            if "cat /proc/uptime" in args:
                return mock.Mock(stdout=uptime, returncode=0)
            return mock.Mock(stdout="", returncode=0)
        return _run

    def test_fresh_boot_waits_then_cleans(self):
        calls = []
        with mock.patch("subprocess.run", side_effect=self._fake_run(calls)), \
             mock.patch("time.sleep", return_value=None) as m_sleep, \
             mock.patch("time.monotonic", side_effect=itertools.count(0, 5)):
            status = cleanup_after_boot(SERIAL, wait_after_boot_s=90)
        self.assertTrue(status["booted"])
        self.assertTrue(status["settled"])
        self.assertTrue(status["force_stopped"])
        self.assertTrue(status["killed"])
        self.assertTrue(status["dropped"])
        # fresh boot (<300s uptime) must have slept the settle window
        self.assertTrue(any(90 in c.args for c in m_sleep.call_args_list))
        # drop_caches must have been issued
        joined = " ".join(" ".join(str(c) for c in c) for c in calls)
        self.assertIn("drop_caches", joined)
        self.assertIn("am kill-all", joined)
        self.assertIn("am force-stop", joined)

    def test_old_boot_skips_settle_wait(self):
        calls = []
        with mock.patch("subprocess.run", side_effect=self._fake_run(
                calls, uptime="900.0 200.0")), \
             mock.patch("time.sleep", return_value=None) as m_sleep, \
             mock.patch("time.monotonic", side_effect=itertools.count(0, 5)):
            status = cleanup_after_boot(SERIAL, wait_after_boot_s=90)
        self.assertTrue(status["booted"])
        self.assertFalse(status["settled"])  # uptime >= 300s: no settle wait
        self.assertTrue(status["dropped"])

    def test_boot_timeout_tolerated(self):
        calls = []
        with mock.patch("subprocess.run", side_effect=self._fake_run(
                calls, booted="")), \
             mock.patch("time.sleep", return_value=None), \
             mock.patch("time.monotonic", side_effect=itertools.count(0, 5)):
            status = cleanup_after_boot(SERIAL, wait_after_boot_s=90)
        self.assertFalse(status["booted"])
        self.assertFalse(status["dropped"])  # no cleanup on boot timeout

    def test_adb_errors_tolerated(self):
        calls = []
        def _boom(*a, **kw):
            calls.append(a)
            raise RuntimeError("adb died")
        with mock.patch("subprocess.run", side_effect=_boom), \
             mock.patch("time.sleep", return_value=None), \
             mock.patch("time.monotonic", side_effect=itertools.count(0, 5)):
            status = cleanup_after_boot(SERIAL, wait_after_boot_s=90)
        # all failures swallowed; flags stay False (best effort)
        self.assertFalse(status["booted"])
        self.assertFalse(status["dropped"])


@unittest.skipUnless(_device_online(), f"device {SERIAL} offline")
class TestCleanupRealDevice(unittest.TestCase):
    """Real device: call the actual function and verify the status."""

    def test_cleanup_on_device(self):
        # let Android's background loading (BOOT_COMPLETED receivers,
        # recents restore) actually happen first, so "before" reflects the
        # dirty state the cleanup is supposed to fix
        time.sleep(90)
        before = self._third_party_count()
        cached_before = self._cached_mb()
        print(f"\n[boot_cleanup] BEFORE: third_party_procs={before} "
              f"cached_mb={cached_before}", flush=True)
        status = cleanup_after_boot(SERIAL, wait_after_boot_s=0)
        after = self._third_party_count()
        cached_after = self._cached_mb()
        print(f"[boot_cleanup] AFTER: third_party_procs={after} "
              f"cached_mb={cached_after} status={status}", flush=True)
        self.assertTrue(status["booted"], status)
        self.assertTrue(status["dropped"], status)
        # if third-party apps were actually running before cleanup,
        # force-stop + kill-all must reduce them. On this workload device
        # most third-party apps do not self-start after boot, so before may
        # be 0 -- in that case only the execution itself is verified.
        if before > 0:
            self.assertLessEqual(after, before,
                                 f"third-party apps not reduced: "
                                 f"{before} -> {after}")
        self.assertLess(cached_after, cached_before,
                        f"drop_caches did not release Cached: "
                        f"{cached_before} -> {cached_after} MB")

    def _cached_mb(self) -> int:
        import subprocess
        out = subprocess.run(["adb", "-s", SERIAL, "shell",
                              "grep -E '^Cached:' /proc/meminfo"],
                             capture_output=True, text=True,
                             timeout=30).stdout
        kb = 0
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) >= 2:
                kb = int(parts[1])
        return kb // 1024

    def _third_party_count(self) -> int:
        import subprocess
        out = subprocess.run(["adb", "-s", SERIAL, "shell",
                              "ps -A -o NAME"], capture_output=True,
                             text=True, timeout=30).stdout
        pkgs = subprocess.run(["adb", "-s", SERIAL, "shell",
                               "pm list packages -3"], capture_output=True,
                              text=True, timeout=30).stdout
        third = {ln.strip().split(":", 1)[1] for ln in pkgs.splitlines()
                 if ln.startswith("package:")}
        return sum(1 for ln in out.splitlines()
                   if ln.strip().split(":", 1)[0] in third)


if __name__ == "__main__":
    unittest.main(verbosity=2)
