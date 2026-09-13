"""组装模型单元测试：default_sample_config.json 模板（统一 probe 采样）+
manifest 差异深合并；probes 按 name 合并；interval_s 优先级测试。"""
from __future__ import annotations

import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
import sys
sys.path.insert(0, str(SRC))

from exp_framework.experiment.config import (load_default_sample_config,
                                             resolve_sample_config)


def probe_by_name(cs, name):
    for p in cs["probes"]:
        if p["name"] == name:
            return p
    raise AssertionError(f"probe {name} missing")


class TestSampleConfigMerge(unittest.TestCase):
    def setUp(self):
        self.tpl = load_default_sample_config()

    def test_template_has_all_cycle_sample_probes(self):
        cs = self.tpl["cycle_sample"]
        for name in ("counters", "vmstat", "buddyinfo", "thermal",
                     "cpufreq", "pagetypeinfo"):
            self.assertIn(name, [p["name"] for p in cs["probes"]],
                          f"cycle_sample 缺 {name} probe")

    def test_vmstat_keys_from_template(self):
        keys = probe_by_name(self.tpl["cycle_sample"], "vmstat")["keys"]
        self.assertGreaterEqual(len(keys), 200)
        self.assertIn("allocstall_normal", keys)
        self.assertIn("pgsteal_order2_kswapd", keys)
        self.assertIn("kswapd_order2_iters_b16_inf", keys)

    def test_counters_defaults_in_template(self):
        p = probe_by_name(self.tpl["cycle_sample"], "counters")
        self.assertIn("anon_fault_fallback", p["keys"])
        self.assertEqual(p["parse"], "kv")
        self.assertEqual(p["out"], "raw_samples.csv")

    def test_probe_interval_from_global(self):
        """只设全局 interval、probe 不写 interval_s → 用全局值。"""
        merged = resolve_sample_config({"cycle_sample": {"interval_s": 20,
                                                         "probes": [{"name": "vmstat"}]}})
        np_ = probe_by_name(merged["cycle_sample"], "vmstat")
        self.assertEqual(np_["interval_s"], 20)
        self.assertEqual(np_["keys"],
                         probe_by_name(self.tpl["cycle_sample"], "vmstat")["keys"])

    def test_probe_interval_override(self):
        """probe 显式 interval_s 覆盖全局。"""
        merged = resolve_sample_config({"cycle_sample": {"interval_s": 20,
                                                         "probes": [{"name": "vmstat",
                                                                     "interval_s": 5}]}})
        self.assertEqual(probe_by_name(merged["cycle_sample"], "vmstat")["interval_s"], 5)

    def test_probe_enable_from_diff_by_name(self):
        merged = resolve_sample_config({"cycle_sample": {"probes": [
            {"name": "thermal", "enabled": True}]}})
        t = probe_by_name(merged["cycle_sample"], "thermal")
        self.assertTrue(t["enabled"])
        self.assertEqual(probe_by_name(
            self.tpl["cycle_sample"], "thermal")["keys"], t["keys"])

    def test_master_gate_in_template(self):
        self.assertTrue(self.tpl.get("enabled", True))
        merged = resolve_sample_config({"enabled": False})
        self.assertFalse(merged["enabled"])

    def test_new_probe_appended(self):
        merged = resolve_sample_config({"cycle_sample": {"probes": [
            {"name": "ptest", "kind": "file",
             "path": "/proc/meminfo", "parse": "kv",
             "out": "ptest_samples.csv", "enabled": False, "interval_s": 10}]}})
        names = [p["name"] for p in merged["cycle_sample"]["probes"]]
        self.assertIn("ptest", names)
        self.assertIn("pagetypeinfo", names)

    def test_empty_diff_equals_template(self):
        merged = resolve_sample_config({})
        self.assertEqual(merged, self.tpl)

    def test_non_cycle_domains_kept(self):
        merged = resolve_sample_config(
            {"tasktime": {"procs": ["kswapd0"], "strict": False}})
        self.assertEqual(merged["tasktime"]["procs"], ["kswapd0"])
        self.assertFalse(merged["tasktime"]["strict"])
        self.assertEqual(merged["power"]["odpm"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)