"""VIRTUAL-SKIN 合成温度模型单测：公式还原验证 + NaN 路径。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from exp_framework.utils.thermal_model import (compute_virtual_skin,
                                               virtual_skin_series)


class TestVirtualSkin(unittest.TestCase):
    def test_formula_reproduced(self):
        """用 16K R1 实测峰值数据验证公式（预期 VIRTUAL-SKIN ≈ 48.8°C）。"""
        temps = {
            "thermal_zone16": 35.4,   # quiet
            "thermal_zone17": 47.4,   # qi
            "thermal_zone19": 34.2,   # usb
            "thermal_zone20": 34.5,   # disp
            "thermal_zone22": 49.2,   # gnss
            "thermal_zone25": 32.8,   # battery
        }
        skin = compute_virtual_skin(temps)
        # VIRTUAL-QI-GNSS = 0.25*47.4 + 0.75*49.2 = 48.75（最大）
        self.assertAlmostEqual(skin, 48.75, places=2)

    def test_quiet_batt_amplifier(self):
        """VIRTUAL-QUIET-BATT 负权重：quiet 明显高于 battery 时放大。"""
        temps = {
            "thermal_zone16": 50.0, "thermal_zone17": 30.0,
            "thermal_zone19": 30.0, "thermal_zone20": 30.0,
            "thermal_zone22": 30.0, "thermal_zone25": 35.0,
        }
        skin = compute_virtual_skin(temps)
        # VIRTUAL-QUIET-BATT = 2.15*50 - 1.15*35 = 107.5 - 40.25 = 67.25（最大）
        self.assertAlmostEqual(skin, 67.25, places=2)

    def test_missing_sensor_returns_nan(self):
        temps = {
            "thermal_zone16": 35.0, "thermal_zone17": 40.0,
            "thermal_zone19": 34.0, "thermal_zone20": 34.0,
            "thermal_zone22": 45.0,  # 缺 battery
        }
        skin = compute_virtual_skin(temps)
        self.assertTrue(skin != skin, "缺传感器应返回 NaN")

    def test_alias_keys_accepted(self):
        temps = {"quiet": 35.0, "qi": 40.0, "usb": 34.0,
                 "disp": 34.0, "gnss": 45.0, "battery": 32.0}
        skin = compute_virtual_skin(temps)
        self.assertFalse(skin != skin)
        self.assertGreater(skin, 0)

    def test_series_from_csv(self):
        """从 thermal_samples.csv 算序列（无 CSV 时跳过）。"""
        import glob
        csvs = glob.glob(
            "/home/nzzhao/learn_os/.worklog/exp_data/*/thermal_samples.csv")
        csvs = [c for c in csvs if "temp_zone17" in open(c).readline()]
        if not csvs:
            self.skipTest("无含合成源的 CSV")
        ts, skins = virtual_skin_series(csvs[0])
        self.assertGreater(len(skins), 0)
        self.assertTrue(all(s == s for s in skins), "存在 NaN 样本")


if __name__ == "__main__":
    unittest.main(verbosity=2)
