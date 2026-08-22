"""CPU time accounting: schedstat snapshots + ftrace mm instance for direct reclaim/compact timing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from . import adb_utils


# ============ schedstat ============

def read_schedstat(serial: str) -> Dict[str, Dict[str, int]]:
    """Read schedstat for kcompactd and kswapd.
    
    Returns: {
        "kcompactd": {"on_cpu_ns": ..., "wait_ns": ..., "timeslices": ..., "pid": ...},
        "kswapd": {"on_cpu_ns": ..., "wait_ns": ..., "timeslices": ..., "pid": ...},
    }
    """
    import subprocess
    result = {}
    
    for name in ["kcompactd", "kswapd"]:
        # Find pid
        cp = subprocess.run(
            ["adb", "-s", serial, "shell", f"ps -A | grep {name} | head -1"],
            capture_output=True, text=True, timeout=15)
        parts = cp.stdout.split()
        if len(parts) < 2:
            continue
        pid = parts[1]
        
        # Read schedstat (world-readable, no su needed)
        cp = subprocess.run(
            ["adb", "-s", serial, "shell", f"cat /proc/{pid}/schedstat"],
            capture_output=True, text=True, timeout=10)
        vals = cp.stdout.strip().split()
        if len(vals) >= 3:
            result[name] = {
                "pid": int(pid),
                "on_cpu_ns": int(vals[0]),
                "wait_ns": int(vals[1]),
                "timeslices": int(vals[2]),
            }

    return result


def save_schedstat(serial: str, out_path: Path) -> Dict:
    data = read_schedstat(serial)
    out_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


# ============ ftrace mm instance ============

FTRACE_INSTANCE = "mm_trace"
FTRACE_EVENTS = [
    "vmscan/mm_vmscan_direct_reclaim_begin",
    "vmscan/mm_vmscan_direct_reclaim_end",
    "compaction/mm_compaction_begin",
    "compaction/mm_compaction_end",
]


def _su_cmd(serial: str, cmd: str, timeout_s: int = 10):
    """Run a single su command via adb without extra sh -c wrapping."""
    import subprocess
    adb_utils.adb_shell_root(serial, cmd, timeout_s=timeout_s, check=False)


def setup_ftrace_mm_instance(serial: str, buffer_kb: int = 16384) -> bool:
    """Create ftrace instance and enable mm events. Returns True on success."""
    base = f"/sys/kernel/tracing/instances/{FTRACE_INSTANCE}"
    
    cmds = [
        f"mkdir {base}",
        f"echo {buffer_kb} > {base}/buffer_size_kb",
        f"echo 0 > {base}/tracing_on",
        f"echo > {base}/trace",
    ]
    for ev in FTRACE_EVENTS:
        cmds.append(f"echo 1 > {base}/events/{ev}/enable")
    cmds.append(f"echo 1 > {base}/tracing_on")
    
    for cmd in cmds:
        _su_cmd(serial, cmd)
    
    # Verify
    import subprocess
    cp = subprocess.run(
        ["adb", "-s", serial, "shell", f"cat {base}/tracing_on"],
        capture_output=True, text=True, timeout=5)
    return cp.stdout.strip() == "1"


def stop_ftrace_mm_instance(serial: str):
    """Stop tracing."""
    base = f"/sys/kernel/tracing/instances/{FTRACE_INSTANCE}"
    _su_cmd(serial, f"echo 0 > {base}/tracing_on")


def pull_ftrace_mm_trace(serial: str, out_path: Path) -> int:
    """Pull trace data and save to file. Returns number of lines."""
    import subprocess
    base = f"/sys/kernel/tracing/instances/{FTRACE_INSTANCE}"
    from exp_framework.utils import adb_utils
    out = adb_utils.adb_shell_root(serial, f"cat {base}/trace",
                                   timeout_s=120, check=False)
    out_path.write_text(cp.stdout, encoding="utf-8")
    return len(cp.stdout.splitlines())


def cleanup_ftrace_mm_instance(serial: str):
    """Remove ftrace instance."""
    base = f"/sys/kernel/tracing/instances/{FTRACE_INSTANCE}"
    _su_cmd(serial, f"echo 0 > {base}/tracing_on")
    for ev in FTRACE_EVENTS:
        _su_cmd(serial, f"echo 0 > {base}/events/{ev}/enable")
    _su_cmd(serial, f"rmdir {base}")


def parse_direct_reclaim_time(trace_path: Path) -> Dict[str, float]:
    """Parse ftrace output to get direct reclaim and compact timing with coverage.

    Coverage = wall-clock time where at least 1 process is in that state.
    This is the real "app is being blocked" time from system perspective.

    Returns dict with per-category: total_ms, count, avg_ms, coverage_ms,
    coverage_pct, max_depth, and overall wall_time_ms.
    """
    reclaim_starts = {}  # pid -> timestamp
    compact_starts = {}  # pid -> timestamp

    total_reclaim_s = 0.0
    total_compact_s = 0.0
    reclaim_count = 0
    compact_count = 0

    reclaim_events = []  # (ts, +1/-1)
    compact_events = []

    first_ts = None
    last_ts = None

    with trace_path.open("r") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            try:
                parts = line.split()
                ts_str = None
                for p in parts:
                    if p.endswith(":") and "." in p and p[0].isdigit():
                        ts_str = p.rstrip(":")
                        break
                if not ts_str:
                    continue
                ts = float(ts_str)

                if first_ts is None:
                    first_ts = ts
                last_ts = ts

                comm_pid = parts[0]
                pid = int(comm_pid.rsplit("-", 1)[1])

                if "mm_vmscan_direct_reclaim_begin" in line:
                    reclaim_starts[pid] = ts
                    reclaim_events.append((ts, 1))
                elif "mm_vmscan_direct_reclaim_end" in line:
                    if pid in reclaim_starts:
                        total_reclaim_s += ts - reclaim_starts[pid]
                        reclaim_count += 1
                        reclaim_events.append((ts, -1))
                        del reclaim_starts[pid]
                elif "mm_compaction_begin" in line:
                    compact_starts[pid] = ts
                    compact_events.append((ts, 1))
                elif "mm_compaction_end" in line:
                    if pid in compact_starts:
                        total_compact_s += ts - compact_starts[pid]
                        compact_count += 1
                        compact_events.append((ts, -1))
                        del compact_starts[pid]
            except (ValueError, IndexError):
                continue

    def calc_coverage(events):
        if not events:
            return 0.0, 0
        events.sort(key=lambda x: x[0])
        depth = 0
        max_depth = 0
        covered_start = None
        total_covered_s = 0.0
        last_end_ts = events[0][0]  # track last -1 event timestamp

        for ts, delta in events:
            depth += delta
            if depth < 0:
                depth = 0
            max_depth = max(max_depth, depth)
            if delta > 0 and depth == 1:
                covered_start = ts
            elif delta < 0 and depth == 0:
                if covered_start is not None:
                    total_covered_s += ts - covered_start
                    covered_start = None
            if delta < 0:
                last_end_ts = ts

        # Do NOT count unclosed coverage (unmatched begins at trace end)
        # covered_start is still set means depth never went back to 0
        # We ignore this tail — it's artifact of trace stopping mid-reclaim

        return total_covered_s * 1000.0, max_depth

    reclaim_coverage_ms, reclaim_max_depth = calc_coverage(reclaim_events)
    compact_coverage_ms, compact_max_depth = calc_coverage(compact_events)

    wall_time_ms = (last_ts - first_ts) * 1000.0 if (first_ts and last_ts) else 0.0

    return {
        "direct_reclaim_total_ms": total_reclaim_s * 1000.0,
        "direct_reclaim_count": reclaim_count,
        "direct_reclaim_avg_ms": (total_reclaim_s * 1000.0 / reclaim_count) if reclaim_count else 0,
        "direct_reclaim_coverage_ms": reclaim_coverage_ms,
        "direct_reclaim_coverage_pct": (reclaim_coverage_ms / wall_time_ms * 100) if wall_time_ms else 0,
        "direct_reclaim_max_depth": reclaim_max_depth,
        "direct_compact_total_ms": total_compact_s * 1000.0,
        "direct_compact_count": compact_count,
        "direct_compact_avg_ms": (total_compact_s * 1000.0 / compact_count) if compact_count else 0,
        "direct_compact_coverage_ms": compact_coverage_ms,
        "direct_compact_coverage_pct": (compact_coverage_ms / wall_time_ms * 100) if wall_time_ms else 0,
        "direct_compact_max_depth": compact_max_depth,
        "wall_time_ms": wall_time_ms,
    }
