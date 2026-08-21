from __future__ import annotations

import sys
import unittest
from pathlib import Path


SKILL_SCRIPTS = Path("/home/nzzhao/.agents/skills/android-thp-fallback-sampler/scripts")
sys.path.insert(0, str(SKILL_SCRIPTS))

from utils.crash_signature import TargetCrashSignatureDetector


def _feed(detector: TargetCrashSignatureDetector, lines: list[str]):
    payload = None
    for line in lines:
        payload = detector.process_line(line)
        if payload is not None:
            return payload
    return payload


class CrashSignatureTests(unittest.TestCase):
    def test_ignores_unrelated_system_cnfe_crashes(self) -> None:
        detector = TargetCrashSignatureDetector(
            serial="SERIAL",
            target_packages={"com.google.android.GoogleCamera", "com.tencent.mm"},
            window_lines=5,
        )

        payload = _feed(
            detector,
            [
                "04-18 20:51:36.900  1452  2723 I am_crash: [4116,0,com.google.android.gms.persistent,-1,java.lang.NoSuchMethodError,foo,Bar.java,16,0]",
                "04-18 20:51:38.153  1452  1768 I am_crash: [2727,0,com.google.android.permissioncontroller,1,java.lang.ClassNotFoundException,androidx.collection.ArraySet,PerformanceTracker.java,25,0]",
            ],
        )

        self.assertIsNone(payload)

    def test_detects_target_package_cnfe_crash(self) -> None:
        detector = TargetCrashSignatureDetector(
            serial="SERIAL",
            target_packages={"com.google.android.GoogleCamera", "com.tencent.mm"},
            window_lines=5,
        )

        payload = _feed(
            detector,
            [
                "04-18 20:51:38.153  1452  1768 I am_crash: [2727,0,com.tencent.mm,1,java.lang.ClassNotFoundException,androidx.collection.ArraySet,PerformanceTracker.java,25,0]",
            ],
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["matched_package"], "com.tencent.mm")
        self.assertEqual(payload["reason"], "target package am_crash with classloading exception")


    def test_ignores_target_npe_plus_unrelated_cnfe(self) -> None:
        """The old false positive: target pkg has NPE, different process has CNFE."""
        detector = TargetCrashSignatureDetector(
            serial="SERIAL",
            target_packages={"com.MobileTicket", "com.youku.phone"},
            window_lines=500,
        )

        payload = _feed(
            detector,
            [
                "06-14 09:24:02.276  1425  2525 I am_crash: [28566,0,com.MobileTicket,821575236,java.lang.NullPointerException,Attempt to invoke...,NULL,23,0]",
                "06-14 09:24:03.575 25382 28967 E OneService: ClassNotFoundException: com.youku.hihonor.provider_impl.HiHonorWhiteBoxProviderImpl",
            ],
        )

        self.assertIsNone(payload)


if __name__ == "__main__":
    unittest.main()
