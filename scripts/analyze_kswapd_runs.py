#!/usr/bin/env python3
"""Analyze a captured kswapd wake/sleep trace -> per-run duration distribution.

Input: trace file from the trace probe (e.g. <out>/trace_kswapd.txt) containing
vmscan:mm_vmscan_kswapd_wake and vmscan:mm_vmscan_kswapd_sleep events.

Pairing: for each node id, a wake event opens a run; the next sleep event with
the same node id closes it. run_us = sleep_ts - wake_ts.

Output (stdout): n, mean/p50/p90/p95/max in ms, plus a small histogram.

Usage:
    python3 scripts/analyze_kswapd_runs.py <trace_file>
"""
from __future__ import annotations

import re
import sys
from typing import Dict, List

_ROW = re.compile(
    r"^\s*\S+\s+\[\d+\]\s+\S+\s+(?P<ts>\d+\.\d+):\s+"
    r"(?P<event>\w+):.*nid=(?P<nid>\d+)")

WAKE = "mm_vmscan_kswapd_wake"
SLEEP = "mm_vmscan_kswapd_sleep"


def parse(path: str):
    runs: Dict[int, List[float]] = {}
    open_ts: Dict[int, float] = {}
    for line in open(path, encoding="utf-8", errors="ignore"):
        m = _ROW.match(line)
        if not m:
            continue
        event, nid, ts = m.group("event"), int(m.group("nid")), float(m.group("ts"))
        if event == WAKE:
            open_ts[nid] = ts
        elif event == SLEEP and nid in open_ts:
            runs.setdefault(nid, []).append((ts - open_ts[nid]) * 1000.0)
            open_ts.pop(nid)
    return runs


def _pct(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * p))
    return sorted_vals[idx]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    runs = parse(sys.argv[1])
    if not runs:
        print("no kswapd wake/sleep pairs found")
        return 1
    print(f"kswapd run durations (ms) per node:")
    all_runs: List[float] = []
    for nid in sorted(runs):
        vals = sorted(runs[nid])
        all_runs.extend(vals)
        print(f"  nid={nid}: n={len(vals)} mean={sum(vals)/len(vals):.1f} "
              f"p50={_pct(vals, 0.5):.1f} p90={_pct(vals, 0.9):.1f} "
              f"p95={_pct(vals, 0.95):.1f} max={vals[-1]:.1f}")
    vals = sorted(all_runs)
    print(f"all : n={len(vals)} mean={sum(vals)/len(vals):.1f} "
          f"p50={_pct(vals, 0.5):.1f} p90={_pct(vals, 0.9):.1f} "
          f"p95={_pct(vals, 0.95):.1f} max={vals[-1]:.1f}")
    # histogram: 0-1,1-5,5-10,10-50,50-100,100+ ms
    buckets = [(0, 1), (1, 5), (5, 10), (10, 50), (50, 100), (100, float("inf"))]
    print("histogram (ms):")
    for lo, hi in buckets:
        n = sum(1 for v in vals if lo <= v < hi)
        print(f"  [{lo:>3},{hi if hi != float('inf') else 'inf':>3}) {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
