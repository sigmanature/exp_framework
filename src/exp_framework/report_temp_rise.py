"""过热实验报告：从 thermal_samples.csv 提取各热区起止温度与升温。

用法：python3 scripts/report_temp_rise.py --out-dir <实验输出目录>
输出：每个 temp_* 列的首行（start）、末行（end）、升温（end - start）、
以及距 115°C 过热保护阈值的余量。
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, List, Optional

OVERHEAT_TRIP_C = 115.0


def load_thermal_csv(csv_path: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    if not csv_path.exists():
        return rows
    with csv_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def temp_columns(rows: List[Dict[str, str]]) -> List[str]:
    cols: List[str] = []
    if not rows:
        return cols
    for c in rows[0]:
        if c.startswith("temp_"):
            cols.append(c)
    return cols


def report(out_dir: Path) -> int:
    csv_path = out_dir / "thermal_samples.csv"
    rows = load_thermal_csv(csv_path)
    if len(rows) < 1:
        print(f"[temp_rise] {csv_path} 不存在或无样本（检查 sample_config.cycle_sample.thermal.enabled）",
              file=sys.stderr)
        return 1

    cols = temp_columns(rows)
    print(f"[temp_rise] {csv_path.name}: {len(rows)} 样本, {len(cols)} 个热区")
    print(f"{'zone':<12s} {'start_C':>8s} {'end_C':>8s} {'rise_C':>8s} "
          f"{'trip_margin_C':>12s}")
    first, last = rows[0], rows[-1]
    for c in sorted(cols):
        try:
            start = float(first[c])
            end = float(last[c])
        except (KeyError, ValueError):
            continue
        rise = end - start
        margin = OVERHEAT_TRIP_C - end
        print(f"{c:<12s} {start:8.1f} {end:8.1f} {rise:+8.1f} {margin:12.1f}")

    peak = 0.0
    peak_col = ""
    for c in cols:
        try:
            v = max(float(r[c]) for r in rows if r[c])
        except (KeyError, ValueError):
            continue
        if v > peak:
            peak, peak_col = v, c
    print(f"[temp_rise] 峰值: {peak_col}={peak:.1f}°C "
          f"(过热保护阈值 {OVERHEAT_TRIP_C:.0f}°C, "
          f"余量 {OVERHEAT_TRIP_C - peak:.1f}°C)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="thermal_samples.csv 升温报告")
    p.add_argument("--out-dir", required=True, help="实验输出目录")
    args = p.parse_args(argv)
    return report(Path(args.out_dir))


if __name__ == "__main__":
    sys.exit(main())
