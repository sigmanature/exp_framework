#!/usr/bin/env python3
"""Standalone CPU accounting: schedstat + ftrace mm trace.

Usage (wrap around memstress):
  # Start tracing before workload:
  python3 trace_cpu_accounting.py start --serial 18281FDF6007HB --out-dir /path/to/run

  # (run memstress here)

  # Stop and collect after workload:
  python3 trace_cpu_accounting.py stop --serial 18281FDF6007HB --out-dir /path/to/run

  # Analyze collected data:
  python3 trace_cpu_accounting.py analyze --out-dir /path/to/run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.cpu_accounting import (
    save_schedstat,
    setup_ftrace_mm_instance,
    stop_ftrace_mm_instance,
    pull_ftrace_mm_trace,
    cleanup_ftrace_mm_instance,
    parse_direct_reclaim_time,
)


def cmd_start(args):
    serial = args.serial
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[trace] Saving schedstat_start...")
    data = save_schedstat(serial, out_dir / "schedstat_start.json")
    for name, vals in data.items():
        print(f"  {name}: pid={vals['pid']} on_cpu={vals['on_cpu_ns']/1e9:.2f}s")

    print(f"[trace] Setting up ftrace mm instance...")
    ok = setup_ftrace_mm_instance(serial, buffer_kb=args.buffer_kb)
    print(f"  ftrace instance ready: {ok}")
    if not ok:
        print("  WARNING: ftrace setup failed, direct reclaim timing will not be available")
    
    print(f"[trace] START done. Run your workload now.")


def cmd_stop(args):
    serial = args.serial
    out_dir = Path(args.out_dir)

    print(f"[trace] Stopping ftrace...")
    stop_ftrace_mm_instance(serial)

    print(f"[trace] Saving schedstat_end...")
    data = save_schedstat(serial, out_dir / "schedstat_end.json")
    for name, vals in data.items():
        print(f"  {name}: pid={vals['pid']} on_cpu={vals['on_cpu_ns']/1e9:.2f}s")

    print(f"[trace] Pulling ftrace trace...")
    lines = pull_ftrace_mm_trace(serial, out_dir / "ftrace_mm.txt")
    print(f"  {lines} lines pulled")

    print(f"[trace] Cleaning up ftrace instance...")
    cleanup_ftrace_mm_instance(serial)

    print(f"[trace] STOP done. Run 'analyze' to parse results.")


def cmd_analyze(args):
    out_dir = Path(args.out_dir)

    # schedstat delta
    ss_path = out_dir / "schedstat_start.json"
    se_path = out_dir / "schedstat_end.json"
    if ss_path.exists() and se_path.exists():
        ss = json.loads(ss_path.read_text())
        se = json.loads(se_path.read_text())
        print("=== CPU Time (schedstat delta) ===")
        for name in ['kcompactd', 'kswapd']:
            if name in ss and name in se:
                cpu_ms = (se[name]['on_cpu_ns'] - ss[name]['on_cpu_ns']) / 1e6
                wait_ms = (se[name]['wait_ns'] - ss[name]['wait_ns']) / 1e6
                slices = se[name]['timeslices'] - ss[name]['timeslices']
                print(f"  {name}: on_cpu={cpu_ms:.0f}ms  run_wait={wait_ms:.0f}ms  slices={slices}")
        print()
    else:
        print("  schedstat files not found")

    # ftrace direct reclaim/compact
    trace_path = out_dir / "ftrace_mm.txt"
    if trace_path.exists() and trace_path.stat().st_size > 100:
        print("=== Direct Reclaim / Compact Timing (ftrace) ===")
        stats = parse_direct_reclaim_time(trace_path)
        print(f"  direct_reclaim: total={stats['direct_reclaim_total_ms']:.0f}ms  count={stats['direct_reclaim_count']}")
        print(f"  direct_compact: total={stats['direct_compact_total_ms']:.0f}ms  count={stats['direct_compact_count']}")
        
        # Save parsed stats
        (out_dir / "direct_reclaim_stats.json").write_text(
            json.dumps(stats, indent=2) + "\n", encoding="utf-8")
        print(f"  Written: {out_dir / 'direct_reclaim_stats.json'}")
    else:
        print("  ftrace_mm.txt not found or empty")


def main():
    p = argparse.ArgumentParser(description="CPU accounting: schedstat + ftrace mm")
    sub = p.add_subparsers(dest="command", required=True)

    sp_start = sub.add_parser("start", help="Start tracing (before workload)")
    sp_start.add_argument("--serial", required=True)
    sp_start.add_argument("--out-dir", required=True)
    sp_start.add_argument("--buffer-kb", type=int, default=16384)

    sp_stop = sub.add_parser("stop", help="Stop tracing and collect (after workload)")
    sp_stop.add_argument("--serial", required=True)
    sp_stop.add_argument("--out-dir", required=True)

    sp_analyze = sub.add_parser("analyze", help="Parse collected trace data")
    sp_analyze.add_argument("--out-dir", required=True)

    args = p.parse_args()
    if args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "analyze":
        cmd_analyze(args)


if __name__ == "__main__":
    main()
