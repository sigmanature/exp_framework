"""组装模型单元测试：default_sample_config.json 模板 + manifest 差异深合并。"""
from __future__ import annotations

import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
import sys
sys.path.insert(0, str(SRC))

from exp_framework.experiment.config import (load_default_sample_config,
                                             resolve_sample_config)


class TestSampleConfigMerge(unittest.TestCase):
    def setUp(self):
        self.tpl = load_default_sample_config()

    def test_template_has_all_cycle_sample_domains(self):
        cs = self.tpl["cycle_sample"]
        for name in ("counters", "vmstat", "buddyinfo", "thermal", "cpufreq"):
            self.assertIn(name, cs, f"cycle_sample 缺 {name} 域")

    def test_vmstat_keys_from_template(self):
        keys = self.tpl["cycle_sample"]["vmstat"]["keys"]
        self.assertEqual(len(keys), 252)
        self.assertIn("allocstall_normal", keys)
        self.assertIn("pgsteal_order2_kswapd", keys)
        self.assertIn("kswapd_order2_iters_b16_inf", keys)

    def test_counters_defaults_in_template(self):
        keys = self.tpl["cycle_sample"]["counters"]["keys"]
        self.assertIn("anon_fault_fallback", keys)
        self.assertEqual(self.tpl["cycle_sample"]["counters"]["out"],
                         "raw_samples.csv")

    def test_diff_overrides_interval(self):
        merged = resolve_sample_config(
            {"cycle_sample": {"vmstat": {"interval_s": 10}}})
        self.assertEqual(merged["cycle_sample"]["vmstat"]["interval_s"], 10)
        self.assertEqual(len(merged["cycle_sample"]["vmstat"]["keys"]), 252)

    def test_enable_thermal_from_diff(self):
        merged = resolve_sample_config(
            {"cycle_sample": {"thermal": {"enabled": True}}})
        self.assertTrue(merged["cycle_sample"]["thermal"]["enabled"])
        self.assertEqual(merged["cycle_sample"]["thermal"]["interval_s"], 10)
        self.assertIn("thermal_zone0", merged["cycle_sample"]["thermal"]["zones"])

    def test_empty_diff_equals_template(self):
        merged = resolve_sample_config({})
        self.assertEqual(merged, self.tpl)

    def test_non_cycle_domains_kept(self):
        merged = resolve_sample_config(
            {"tasktime": {"procs": ["kswapd0"], "strict": False}})
        self.assertEqual(merged["tasktime"]["procs"], ["kswapd0"])
        self.assertFalse(merged["tasktime"]["strict"])
        self.assertEqual(merged["power"]["odpm"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
