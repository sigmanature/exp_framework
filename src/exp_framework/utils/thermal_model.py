"""Pixel thermal HAL 的 VIRTUAL-SKIN 合成温度模型（公式从官方配置还原）。

VIRTUAL-SKIN = MAX(VIRTUAL-QI-GNSS, VIRTUAL-QI-QUIET,
                   VIRTUAL-USB2-DISP, VIRTUAL-QUIET-BATT)    （单位 °C）
权重来自 /vendor/etc/thermal_info_config.json（oriole）：
  VIRTUAL-QI-GNSS    = 0.25×qi_therm      + 0.75×gnss_tcxo_therm
  VIRTUAL-QI-QUIET   = 0.25×qi_therm      + 0.75×quiet_therm
  VIRTUAL-USB2-DISP  = 0.16×usb_pwr_therm2 + 0.84×disp_therm
  VIRTUAL-QUIET-BATT = 2.15×quiet_therm   − 1.15×battery

VIRTUAL-SKIN ≥ 55.0°C 时 thermal HAL 触发 shutdown,thermal（瞬时判定）。
冷却判据用 VIRTUAL-SKIN（计算值）而非单源：合成值才是 HAL 真判定值，
单源（如 quiet_therm）会低估红线（R4/R5 热关机时 quiet 仅 43-44.8°C）。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# 合成源 zone（thermal_zoneN）→ 公式键名
SKIN_SOURCE_ZONES = {
    "thermal_zone16": "quiet",
    "thermal_zone17": "qi",
    "thermal_zone19": "usb",
    "thermal_zone20": "disp",
    "thermal_zone22": "gnss",
    "thermal_zone25": "battery",
}

# VIRTUAL-SKIN 关机红线（thermal HAL 配置，55.0°C 瞬时判定）
VIRTUAL_SKIN_SHUTDOWN_C = 55.0


def compute_virtual_skin(zone_temps: Dict[str, float]) -> float:
    """输入 {zone名: °C}（键支持 'thermal_zone16' 或别名 'quiet'），返回 VIRTUAL-SKIN °C。

    缺任一合成源 → 返回 NaN（无法计算，调用方应视为"未知，按不安全处理"）。
    """
    def _get(alias: str) -> Optional[float]:
        v = zone_temps.get(alias)
        if v is not None:
            return float(v)
        for zone, a in SKIN_SOURCE_ZONES.items():
            if a == alias and zone in zone_temps:
                return float(zone_temps[zone])
        return None

    quiet = _get("quiet")
    qi = _get("qi")
    usb = _get("usb")
    disp = _get("disp")
    gnss = _get("gnss")
    battery = _get("battery")
    if any(v is None for v in (quiet, qi, usb, disp, gnss, battery)):
        return float("nan")

    v_qi_gnss = 0.25 * qi + 0.75 * gnss
    v_qi_quiet = 0.25 * qi + 0.75 * quiet
    v_usb_disp = 0.16 * usb + 0.84 * disp
    v_quiet_batt = 2.15 * quiet - 1.15 * battery
    return max(v_qi_gnss, v_qi_quiet, v_usb_disp, v_quiet_batt)


def read_skin_sources(serial: str) -> Dict[str, float]:
    """一次 adb 调用读 6 个合成源 zone（16/17/19/20/22/25），返回 {zone名: °C}。

    读取失败/超时的 zone 置 NaN（compute_virtual_skin 会因此返回 NaN）。
    """
    from exp_framework.utils import adb_utils

    zones = list(SKIN_SOURCE_ZONES.keys())
    out = adb_utils.adb_shell_root(
        serial,
        "for z in %s; do cat /sys/class/thermal/$z/temp 2>/dev/null; done"
        % " ".join(zones),
        timeout_s=10, check=False)
    vals = [t for t in str(out).split() if t.replace(".", "").isdigit()]
    result: Dict[str, float] = {}
    for i, zone in enumerate(zones):
        if i < len(vals):
            result[zone] = float(vals[i]) / 1000.0
        else:
            result[zone] = float("nan")
    return result


def virtual_skin_series(csv_path) -> Tuple[List[float], List[float]]:
    """从 thermal_samples.csv（host_ts + temp_zone16/17/19/20/22/25 列）算
    VIRTUAL-SKIN 时间序列，返回 (host_ts列表, skin列表)。离线回放验证用。"""
    import csv

    ts: List[float] = []
    skins: List[float] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            temps = {}
            for zone in SKIN_SOURCE_ZONES:
                v = row.get(f"temp_{zone.split('_')[-1]}")
                if v:
                    temps[zone] = float(v)
            if len(temps) == len(SKIN_SOURCE_ZONES):
                ts.append(float(row["host_ts"]))
                skins.append(compute_virtual_skin(temps))
    return ts, skins
