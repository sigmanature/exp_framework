"""run_with_config 编排流功能测试（全 mock，不碰真实设备/真实锁文件）。

验证的核心契约：
- rounds=1/3 与 precondition once/per_round 的调用次数
- 任何终态（正常/失败/占用）都经 _finish_with_lock 收尾：
  锁只在此释放，manifest 终态只在此写出
"""
from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
import sys
sys.path.insert(0, str(SRC))

from exp_framework.experiment.runner import run_with_config


class FakeSession:
    def __init__(self):
        self.sampling_result = {"samples": 1, "errors": 0}
        self.crash_event = threading.Event()


class FakeBackend:
    def __init__(self):
        self.name = "fake"
        self.work_dir = Path("/tmp/fake")
        self.precondition_calls = 0
        self.run_calls = 0
        self.cleanup_calls = 0
        self.fail_on_round_2 = False

    def prepare(self):
        return {"packages_resolved": {}}

    def precondition(self):
        self.precondition_calls += 1
        return {"fragment": {"ok": 1}}

    def assign_work_dir(self, path: Path) -> None:
        self.work_dir = path
        path.mkdir(parents=True, exist_ok=True)

    def run(self):
        self.run_calls += 1
        if self.run_calls == 2 and self.fail_on_round_2:
            raise RuntimeError("boom in round 2")
        return {"total_cycles": 1, "timing": {"mean_cycle_s": 0.1}}

    def stop_device(self):
        pass

    def cleanup(self):
        self.cleanup_calls += 1


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        rounds=None, precondition_mode=None, counters="", interval_s=60,
        no_crash_detect=True, clear_logcat=False, post_prepare_cmd=None,
        post_workload_cmd=None, exp_name=None)


def _input_cfg(rounds=None, precond_mode=None):
    cfg = {"exp_ctx": {"exp_name": "t", "domain": "pixel"},
           "backend": {"name": "fake", "config": {}}}
    if rounds is not None:
        cfg["rounds"] = rounds
    if precond_mode is not None:
        cfg["precondition"] = {"mode": precond_mode}
    return {"config": cfg, "sample_config": {}}


class TestRunnerFlow(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.releases = []          # (state, reason) exp_lock_release 记录
        self.tmpdir = tempfile.mkdtemp(prefix="expf_flow_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _patches(self):
        return [
            mock.patch("exp_framework.experiment.runner.exp_lock_claim",
                       return_value="running"),
            mock.patch("exp_framework.utils.exp_lock.exp_lock_heartbeat",
                       return_value="ok"),
            mock.patch("exp_framework.experiment.runner.reboot_device_and_wait",
                       return_value={"booted": True}),
            mock.patch("exp_framework.experiment.runner.exp_lock_release",
                       side_effect=lambda d, s, e, st, r="":
                       self.releases.append((st, r)) or f"ok:{st}"),
            mock.patch("exp_framework.utils.adb_utils.ensure_privilege"),
            mock.patch("exp_framework.experiment.runner._device_online",
                       return_value=True),
            mock.patch("exp_framework.experiment.runner._device_residual_check",
                       return_value=[]),
            mock.patch("exp_framework.experiment.runner.create_experiment",
                       side_effect=lambda *a, **k: self.backend),
            mock.patch("exp_framework.experiment.runner.sample_start",
                       return_value=FakeSession()),
            mock.patch("exp_framework.experiment.runner.sample_end"),
        ]

    def _run(self, input_cfg, extra_patches=()):
        with contextlib.ExitStack() as stack:
            for p in list(self._patches()) + list(extra_patches):
                stack.enter_context(p)
            return run_with_config("SERIAL", Path(self.tmpdir), input_cfg,
                                   _args(), threading.Event())

    def _final_manifest(self) -> dict:
        """读实验根目录（唯一子目录）下的最终 run_manifest.json。"""
        matches = list(Path(self.tmpdir).glob("*/run_manifest.json"))
        self.assertEqual(len(matches), 1, "实验根目录应恰有一个 run_manifest.json")
        return json.loads(matches[0].read_text("utf-8"))

    def test_default_single_round(self):
        manifest = self._run(_input_cfg())
        self.assertEqual(self.backend.run_calls, 1)
        self.assertEqual(self.backend.precondition_calls, 0)
        self.assertEqual(manifest["status"], "finished")
        self.assertEqual(manifest["rounds_done"], 1)
        self.assertEqual(self.releases, [("done", "exit_code=0")])
        self.assertEqual(self._final_manifest()["status"], "finished")

    def test_three_rounds_loops(self):
        manifest = self._run(_input_cfg(rounds=3))
        self.assertEqual(self.backend.run_calls, 3)
        self.assertEqual(len(manifest["rounds"]), 3)
        self.assertEqual(manifest["samples"], 3)
        self.assertEqual(manifest["status"], "finished")

    def test_precondition_once(self):
        manifest = self._run(_input_cfg(rounds=3, precond_mode="once"))
        self.assertEqual(self.backend.precondition_calls, 1)
        self.assertEqual(self.backend.run_calls, 3)
        self.assertIn("fragment", manifest)

    def test_precondition_per_round(self):
        manifest = self._run(_input_cfg(rounds=3, precond_mode="per_round"))
        self.assertEqual(self.backend.precondition_calls, 3)
        self.assertEqual(self.backend.run_calls, 3)

    def test_precondition_restart_once(self):
        """restart:true -> 框架层先重启再打碎（once 模式共一次）。"""
        self.reboots = []
        extra = [mock.patch(
            "exp_framework.experiment.runner.reboot_device_and_wait",
            side_effect=lambda serial, **kw:
            self.reboots.append(serial) or {"booted": True})]
        manifest = self._run({
            "config": {
                "exp_ctx": {"exp_name": "t", "domain": "pixel"},
                "backend": {"name": "fake", "config": {}},
                "precondition": {"mode": "once", "restart": True},
            },
            "sample_config": {},
        }, extra_patches=extra)
        self.assertEqual(self.reboots, ["SERIAL"])   # 重启先发生一次
        self.assertEqual(self.backend.precondition_calls, 1)
        self.assertIn("fragment", manifest)

    def test_round_failure_reaches_finish(self):
        self.backend.fail_on_round_2 = True
        with self.assertRaises(RuntimeError):
            self._run(_input_cfg(rounds=3))
        # 失败也走 _finish_with_lock：cleanup 执行 + 锁 release(failed)
        self.assertEqual(self.backend.cleanup_calls, 1)
        self.assertEqual(self.releases[-1][0], "failed")
        final = self._final_manifest()
        self.assertEqual(final["status"], "failed")
        self.assertIn("boom", final["stop_reason"]["run_exc"] or "")

    def test_busy_does_not_claim(self):
        with contextlib.ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            stack.enter_context(mock.patch(
                "exp_framework.experiment.runner.exp_lock_claim",
                return_value="rejected:busy"))
            with self.assertRaises(RuntimeError):
                run_with_config("SERIAL", Path(self.tmpdir), _input_cfg(),
                                _args(), threading.Event())
        # 未被拒绝的 claim 上锁不放 -> 不调用 release/cleanup
        self.assertEqual(self.releases, [])
        self.assertEqual(self.backend.cleanup_calls, 0)


if __name__ == "__main__":
    unittest.main()
