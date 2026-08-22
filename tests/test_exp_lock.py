"""exp_lock 串行锁单测：状态机 / FIFO 队列 / 原子性 / 防误释放。

隔离测试环境：CURSOR_FILE 指向临时目录，测试后清理。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import exp_framework.utils.exp_lock as lockmod

DOMAIN, DEVICE = "pixel", "test-device-001"


class ExpLockTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="exp_lock_test_")
        lockmod.CURSOR_FILE = Path(self._tmp) / "run_cursor.json"
        lockmod.LOCK_FILE = Path(self._tmp) / "run_cursor.json.lock"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_claim_running_then_queue(self):
        r1 = lockmod.exp_lock_claim(DOMAIN, DEVICE, "exp-a", "/run/a",
                                    session_id="s-1", agent_tool="opencode")
        self.assertEqual(r1, "running")
        r2 = lockmod.exp_lock_claim(DOMAIN, DEVICE, "exp-b", "/run/b",
                                    session_id="s-2")
        self.assertEqual(r2, "queued:1")
        r3 = lockmod.exp_lock_claim(DOMAIN, DEVICE, "exp-c", "/run/c")
        self.assertEqual(r3, "queued:2")
        e = lockmod.exp_lock_get(DOMAIN, DEVICE)
        self.assertEqual(e["state"], "running")
        self.assertEqual(e["exp_id"], "exp-a")
        self.assertEqual(len(e["queue"]), 2)
        self.assertEqual(lockmod.exp_lock_peek(DOMAIN, DEVICE)["exp_id"], "exp-b")

    def test_release_then_next_claim_replaces_entry_queue_kept(self):
        lockmod.exp_lock_claim(DOMAIN, DEVICE, "exp-a", "/run/a")
        lockmod.exp_lock_claim(DOMAIN, DEVICE, "exp-b", "/run/b")
        self.assertEqual(lockmod.exp_lock_release(
            DOMAIN, DEVICE, "exp-a", "done", "ok"), "ok:done")
        e = lockmod.exp_lock_get(DOMAIN, DEVICE)
        self.assertEqual(e["state"], "done")
        # 新 claim：整体替换当前实验，queue 保留（exp-b 仍在队中）
        r = lockmod.exp_lock_claim(DOMAIN, DEVICE, "exp-x", "/run/x")
        self.assertEqual(r, "running")
        e = lockmod.exp_lock_get(DOMAIN, DEVICE)
        self.assertEqual(e["exp_id"], "exp-x")
        self.assertEqual([q["exp_id"] for q in e["queue"]], ["exp-b"])

    def test_cleanup_failed_blocks_new_claim(self):
        lockmod.exp_lock_claim(DOMAIN, DEVICE, "exp-a", "/run/a")
        lockmod.exp_lock_mark_cleanup_failed(DOMAIN, DEVICE, "exp-a", "残留")
        r = lockmod.exp_lock_claim(DOMAIN, DEVICE, "exp-b", "/run/b")
        self.assertTrue(r.startswith("rejected:cleanup_failed"), r)
        # 人工放行后才能再 claim
        self.assertEqual(lockmod.exp_lock_clean(
            DOMAIN, DEVICE, "exp-a", "已清理"), "ok:released")
        r = lockmod.exp_lock_claim(DOMAIN, DEVICE, "exp-b", "/run/b")
        self.assertEqual(r, "running")

    def test_release_mismatch_rejected(self):
        lockmod.exp_lock_claim(DOMAIN, DEVICE, "exp-a", "/run/a")
        r = lockmod.exp_lock_release(DOMAIN, DEVICE, "exp-b", "done")
        self.assertTrue(r.startswith("rejected:exp_id mismatch"), r)
        # 正确持有者仍可释放
        self.assertEqual(lockmod.exp_lock_release(
            DOMAIN, DEVICE, "exp-a", "done"), "ok:done")

    def test_mark_cleanup_failed_mismatch_rejected(self):
        lockmod.exp_lock_claim(DOMAIN, DEVICE, "exp-a", "/run/a")
        r = lockmod.exp_lock_mark_cleanup_failed(DOMAIN, DEVICE, "exp-b", "x")
        self.assertTrue(r.startswith("rejected:exp_id mismatch"), r)

    def test_heartbeat_only_for_owner(self):
        lockmod.exp_lock_claim(DOMAIN, DEVICE, "exp-a", "/run/a")
        self.assertEqual(lockmod.exp_lock_heartbeat(
            DOMAIN, DEVICE, "exp-a"), "ok")
        self.assertEqual(lockmod.exp_lock_heartbeat(
            DOMAIN, DEVICE, "exp-b"), "warn:not-running")

    def test_poll_until_free(self):
        lockmod.exp_lock_claim(DOMAIN, DEVICE, "exp-a", "/run/a")
        # 占用中：poll 不返回 True（用短超时验证）
        done = lockmod.exp_lock_poll_until_free(
            DOMAIN, DEVICE, poll_s=0.05, max_wait_s=0.3)
        self.assertFalse(done)
        lockmod.exp_lock_release(DOMAIN, DEVICE, "exp-a", "done")
        self.assertTrue(lockmod.exp_lock_poll_until_free(
            DOMAIN, DEVICE, poll_s=0.05, max_wait_s=1))

    def test_concurrent_claim_single_running(self):
        """并发原子性：10 线程同时 claim，只允许 1 个 running，其余排队。"""
        results: dict = {}
        results_lock = threading.Lock()

        def worker(i: int):
            r = lockmod.exp_lock_claim(DOMAIN, DEVICE, f"exp-{i}", f"/run/{i}")
            with results_lock:
                results[f"exp-{i}"] = r

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        running = [k for k, v in results.items() if v == "running"]
        self.assertEqual(len(running), 1, f"running 必须唯一: {results}")
        self.assertEqual(
            sum(1 for v in results.values() if v.startswith("queued:")), 9)
        # 队列无重复
        e = lockmod.exp_lock_get(DOMAIN, DEVICE)
        ids = [q["exp_id"] for q in e["queue"]]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
