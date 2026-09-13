#!/usr/bin/env python3
"""Analyze reclaim rounds from a captured trace.

Pairing model (per earlier design):
  - timestamps are monotonic (probe pins trace_clock=mono); the time axis is
    comparable across CPUs/threads.
  - reclaim rounds are delimited by <begin>/<end> pairs PER THREAD (tid):
    multiple concurrent reclaimers interleave in the buffer, so pairing must
    group by tid and use a per-thread stack (nested begins are legal).
  - round content: aggregate, within [begin_ts, end_ts] of the SAME tid, all
    mm_vmscan_reclaim_pages events: pages += nr_reclaimed, ordbkt[o] += ... .

Events used (configurable):
  begin/end: vmscan:mm_vmscan_direct_reclaim_begin/end
             vmscan:mm_vmscan_memcg_reclaim_begin/end   (kfragd path)
  content : vmscan:mm_vmscan_reclaim_pages              (with ordbkt field)

Output per comm (kswapd0 / kfragd0 / others):
  rounds: n, mean/p50/p90/p95/max duration (ms)
  pages : total reclaimed, mean per round
  order : order0/order1/... pages summed over all rounds
Also reports unmatched begins/ends (trace coverage quality).

Usage:
  python3 scripts/analyze_reclaim_rounds.py <trace_file>
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from typing import Dict, List, Tuple

BEGIN_EVENTS = ("mm_vmscan_direct_reclaim_begin", "mm_vmscan_memcg_reclaim_begin",
                "mm_vmscan_memcg_softlimit_reclaim_begin")
END_EVENTS = ("mm_vmscan_direct_reclaim_end", "mm_vmscan_memcg_reclaim_end",
              "mm_vmscan_memcg_softlimit_reclaim_end")
PAGES_EVENT = "mm_vmscan_reclaim_pages"

_ROW = re.compile(
    r"^\s*(?P<task>\S+)-(?P<tid>\d+)\s+\[\d+\]\s+\S+\s+"
    r"(?P<ts>\d+\.\d+):\s+(?P<event>\w+):.*")

_ORD = re.compile(r"ordbkt=(\d+(?:,\d+)*)")

NR_ORDERS = 11  # NR_PAGE_ORDERS on this kernel


def parse(path: str):
    rounds: Dict[int, List[dict]] = defaultdict(list)  # tid -> [round]
    open_stacks: Dict[int, List[float]] = defaultdict(list)
    pending_pages: Dict[int, List[Tuple[float, int, List[int]]]] = defaultdict(list)
    unmatched_begins = unmatched_ends = 0
    comm_by_tid: Dict[int, str] = {}

    for line in open(path, encoding="utf-8", errors="ignore"):
        m = _ROW.match(line)
        if not m:
            continue
        tid = int(m.group("tid"))
        ts = float(m.group("ts"))
        event = m.group("event")
        comm_by_tid[tid] = m.group("task")
        if event in BEGIN_EVENTS:
            open_stacks[tid].append(ts)
        elif event in END_EVENTS:
            stack = open_stacks[tid]
            if stack:
                begin_ts = stack.pop()
                content = pending_pages[tid]
                pending_pages[tid] = []
                pages = sum(c[1] for c in content)
                ords = [0] * NR_ORDERS
                for _, _, o in content:
                    for i, v in enumerate(o):
                        ords[i] += v
                rounds[tid].append({
                    "begin": begin_ts, "end": ts,
                    "dur_ms": (ts - begin_ts) * 1000.0,
                    "pages": pages, "ords": ords,
                })
            else:
                unmatched_ends += 1
        elif event == PAGES_EVENT:
            mm = _ORD.search(line)
            ords = [int(x) for x in mm.group(1).split(",")] if mm else []
            ords = (ords + [0] * NR_ORDERS)[:NR_ORDERS]
            rp = re.search(r"nr_reclaimed=(\d+)", line)
            pages = int(rp.group(1)) if rp else 0
            pending_pages[tid].append((ts, pages, ords))
    for tid in open_stacks:
        unmatched_begins += len(open_stacks[tid])
    return rounds, comm_by_tid, unmatched_begins, unmatched_ends


def _pct(vals: List[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    rounds, comm_by_tid, ub, ue = parse(sys.argv[1])
    if not rounds:
        print("no reclaim begin/end pairs found")
        return 1
    by_comm: Dict[str, List[dict]] = defaultdict(list)
    for tid, rds in rounds.items():
        by_comm[comm_by_tid.get(tid, f"tid{tid}")].extend(rds)
    print("reclaim rounds per comm:")
    for comm in sorted(by_comm):
        rds = by_comm[comm]
        durs = [r["dur_ms"] for r in rds]
        pages = [r["pages"] for r in rds]
        ords = [0] * NR_ORDERS
        for r in rds:
            for i, v in enumerate(r["ords"]):
                ords[i] += v
        print(f"  {comm}: rounds={len(rds)} "
              f"dur_ms mean={sum(durs)/len(durs):.1f} p50={_pct(durs, .5):.1f} "
              f"p90={_pct(durs, .9):.1f} p95={_pct(durs, .95):.1f} max={max(durs):.1f}")
        print(f"      pages total={sum(pages)} mean/round={sum(pages)/len(pages):.1f}")
        nonz = [(i, v) for i, v in enumerate(ords) if v]
        print(f"      orders: " + ", ".join(f"o{i}={v}" for i, v in nonz) or "      orders: (none)")
    print(f"unmatched begins={ub} ends={ue}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
