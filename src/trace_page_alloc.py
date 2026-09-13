#!/usr/bin/env python3
"""Trace mm_page_alloc (order >= 2) with kernel stack traces during interactive workloads.

Captures full kernel call stacks for every high-order page allocation,
then groups and counts them to identify which drivers trigger the allocations.

Usage:
    python3 scripts/trace_page_alloc.py \
      --serial 21121FDF600C4G \
      --interaction douyin \
      --duration-s 20 \
      --out-dir ./trace_output

Outputs:
    raw_trace.txt      - raw trace buffer content
    analysis.txt       - human-readable analysis (order distribution, top stacks, driver hits)
    stacks.json        - machine-readable stack groups
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from exp_framework.utils.adb_utils import adb_shell_cp, ensure_adb_works
from exp_framework.utils import adb_utils
from exp_framework.utils.interactive import interact_douyin, interact_camera

TRACE_BASE = "/sys/kernel/tracing"
EVENT_DIR = f"{TRACE_BASE}/events/kmem/mm_page_alloc"

# Driver functions we expect to see in stacks for different subsystems.
# Each entry: (label, [function_names_to_match])
EXPECTED_DRIVERS = [
    ("dma-heap (system heap)", ["dmabuf_page_pool_alloc_pages", "system_heap_allocate",
                                 "alloc_largest_available", "dma_heap_buffer_alloc",
                                 "dmabuf_page_pool_alloc"]),
    ("GPU Mali", ["mgm_alloc_page", "kbase_mem_alloc_page",
                   "kbase_mem_pool_alloc_pages"]),
    ("GPU Mali protected", ["mali_pma_alloc_page", "mali_pma_slab_alloc",
                             "mali_pma_slab_add"]),
    ("GPU G2D", ["g2d_create_task"]),
    ("Video codec", ["mfc_mem_dma_heap_alloc", "mfc_mem_special_buf_alloc",
                      "smfc"]),
    ("Video BigOcean", ["bigo", "bigo_iommu"]),
    ("Camera LWIS", ["lwis_platform_dma_buffer_alloc", "lwis_buffer_enroll"]),
    ("WiFi BCM4389", ["dhd_dma_buf_alloc", "dhd_init_wlan_mem",
                       "dhd_msgbuf_rxbuf_post", "dhd_msgbuf", "linux_pktget"]),
    ("USB gadget (adb)", ["ffs_epfile_io", "ffs_epfile_read", "ffs_epfile_write"]),
    ("ION physical", ["ion_physical_heap"]),
    ("THP (anon folio)", ["__alloc_pages_mpol_noprof", "alloc_anon_folio",
                           "folio_alloc_mpol_noprof"]),
    ("Slab allocator", ["allocate_slab", "___slab_alloc", "new_slab"]),
    ("Page cache / filemap", ["page_cache_ra_order", "filemap_alloc_folio"]),
]


class TraceSetupError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Phase 1: setup trace environment
# ---------------------------------------------------------------------------

def setup_trace(serial: str, min_order: int = 2) -> None:
    """Configure ftrace to record mm_page_alloc with order >= min_order and kernel stacks."""

    def _su_write(path: str, value: str) -> None:
        # Use printf to avoid shell interpreting '>' as redirect
        cp = adb_utils.adb_shell_root(serial, f"printf '%s' '{value}' > {path}", timeout_s=10)
        if cp.returncode != 0:
            raise TraceSetupError(f"Failed to write {value!r} to {path}: {cp.stderr}")

    def _su_cat(path: str) -> str:
        cp = adb_utils.adb_shell_root(serial, f"cat {path}", timeout_s=10)
        return (cp.stdout or "").strip()

    # 1. Disable tracing while configuring
    adb_utils.adb_shell_root(serial, f"echo 0 > {TRACE_BASE}/tracing_on", timeout_s=5)
    adb_utils.adb_shell_root(serial, f"echo 0 > {EVENT_DIR}/enable", timeout_s=5)

    # 2. Remove any existing trigger
    adb_utils.adb_shell_root(serial, f"echo '' > {EVENT_DIR}/trigger", timeout_s=5)

    # 3. Set event filter (only record order >= min_order)
    filter_str = f"order >= {min_order}"
    _su_write(f"{EVENT_DIR}/filter", filter_str)
    actual = _su_cat(f"{EVENT_DIR}/filter")
    if filter_str not in actual:
        raise TraceSetupError(f"Filter verification failed: expected '{filter_str}', got '{actual}'")

    # 4. Enable global stacktrace option (per-event trigger is unreliable on Android)
    _su_write(f"{TRACE_BASE}/options/stacktrace", "1")

    # 5. Increase buffer size to hold enough events with stacks
    adb_utils.adb_shell_root(serial, f"echo 8192 > {TRACE_BASE}/buffer_size_kb", timeout_s=5)

    # 6. Clear trace buffer
    adb_utils.adb_shell_root(serial, f"echo > {TRACE_BASE}/trace", timeout_s=5)

    # 7. Enable the event
    adb_utils.adb_shell_root(serial, f"echo 1 > {EVENT_DIR}/enable", timeout_s=5)


def start_tracing(serial: str) -> None:
    adb_utils.adb_shell_root(serial, f"echo 1 > {TRACE_BASE}/tracing_on", timeout_s=5)


def stop_tracing(serial: str) -> None:
    adb_utils.adb_shell_root(serial, f"echo 0 > {TRACE_BASE}/tracing_on", timeout_s=5)


def teardown_trace(serial: str) -> None:
    """Restore trace environment to clean state."""
    adb_utils.adb_shell_root(serial, f"echo 0 > {TRACE_BASE}/tracing_on", timeout_s=5)
    adb_utils.adb_shell_root(serial, f"echo 0 > {EVENT_DIR}/enable", timeout_s=5)
    adb_utils.adb_shell_root(serial, f"echo 0 > {TRACE_BASE}/options/stacktrace", timeout_s=5)
    adb_utils.adb_shell_root(serial, f"printf '%s' '0' > {EVENT_DIR}/filter", timeout_s=5)
    adb_utils.adb_shell_root(serial, f"echo '' > {EVENT_DIR}/trigger", timeout_s=5)


def read_trace(serial: str) -> str:
    """Read the full trace buffer content."""
    cp = adb_utils.adb_shell_root(serial, f"cat {TRACE_BASE}/trace", timeout_s=30)
    return cp.stdout or ""


# ---------------------------------------------------------------------------
# Phase 4: parse and analyze
# ---------------------------------------------------------------------------

EVENT_RE = re.compile(
    r"^\s*(?P<task>[\S]+)-(?P<pid>\d+)\s+"
    r"\[\d+\]\s+\S+\s+"
    r"(?P<ts>\d+\.\d+):\s+"
    r"mm_page_alloc:\s+"
    r".*?\border=(?P<order>\d+)\b"
    r".*?\bgfp_flags=(?P<gfp>[^\n]*)",
)

STACK_FRAME_RE = re.compile(r"^\s+=>\s+(?P<func>\S+)")


def parse_trace(raw: str) -> List[Dict]:
    """Parse raw ftrace output into a list of events, each with a stack trace.

    Each event dict: {order, task, pid, ts, gfp, stack: [func_names]}
    """
    events: List[Dict] = []
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Skip comment lines
        if line.startswith("#"):
            i += 1
            continue

        m = EVENT_RE.match(line)
        if not m:
            i += 1
            continue

        event = {
            "order": int(m.group("order")),
            "task": m.group("task"),
            "pid": int(m.group("pid")),
            "ts": float(m.group("ts")),
            "gfp": m.group("gfp").strip(),
            "stack": [],
        }
        i += 1

        # Collect stack frames until next event or end
        while i < len(lines):
            line = lines[i].strip()
            sm = STACK_FRAME_RE.match(lines[i])
            if sm:
                event["stack"].append(sm.group("func"))
                i += 1
            elif line.startswith("=>"):
                event["stack"].append(line.lstrip("=> "))
                i += 1
            elif line == "<stack trace>" or line == "" or "<stack trace>" in line:
                i += 1
            elif EVENT_RE.match(lines[i]):
                break
            else:
                break

        events.append(event)

    return events


def classify_stack(stack: List[str]) -> List[str]:
    """Return list of driver labels whose functions appear in the stack."""
    hits = []
    stack_text = " ".join(stack)
    for label, funcs in EXPECTED_DRIVERS:
        if any(f in stack_text for f in funcs):
            hits.append(label)
    return hits


def stack_signature(stack: List[str]) -> str:
    """Return a canonical string to group identical stacks.

    Strip offsets and module annotations, keep only function names.
    """
    cleaned = []
    for frame in stack:
        # Remove +0xoffset/0xsize, remove module annotations [module]
        cleaned.append(re.sub(r"\+0x[0-9a-f]+.*$", "", frame).strip())
    return " -> ".join(cleaned)


def analyze(events: List[Dict]) -> Dict:
    """Produce analysis dict from parsed events."""
    if not events:
        return {
            "total_events": 0,
            "order_distribution": {},
            "task_counts": {},
            "top_stacks": [],
            "driver_hits": {},
            "summary": "No events captured. Try longer duration or check filter.",
        }

    # Order distribution
    order_dist: Dict[int, int] = Counter(e["order"] for e in events)

    # Task counts
    task_counts: Dict[str, int] = Counter(e["task"] for e in events)

    # Group by stack signature
    stack_groups: Dict[str, List[Dict]] = defaultdict(list)
    for e in events:
        if e["stack"]:
            sig = stack_signature(e["stack"])
            stack_groups[sig].append(e)

    # Sort stacks by occurrence count
    top_stacks = []
    for sig, group in sorted(stack_groups.items(), key=lambda x: -len(x[1])):
        orders = Counter(e["order"] for e in group)
        drivers = classify_stack(group[0]["stack"])
        top_stacks.append({
            "count": len(group),
            "orders": dict(orders.most_common()),
            "drivers": drivers,
            "sample_stack": group[0]["stack"],
        })

    # Aggregate driver hits
    driver_hits: Dict[str, int] = Counter()
    for e in events:
        for d in classify_stack(e["stack"]):
            driver_hits[d] += 1

    # Overall summary
    parts = [
        f"Total events (order >= 2): {len(events)}",
        f"Order distribution: {dict(sorted(order_dist.items(), key=lambda x: -x[1]))}",
        f"Unique stack signatures: {len(top_stacks)}",
    ]
    if driver_hits:
        parts.append(f"\nDriver hits:")
        for d, count in driver_hits.most_common():
            parts.append(f"  {d}: {count}")
    if top_stacks:
        parts.append(f"\nTop stacks:")
        for i, s in enumerate(top_stacks[:10]):
            drivers_str = f"  [{', '.join(s['drivers'])}]" if s["drivers"] else "  [unknown]"
            parts.append(f"  #{i+1} count={s['count']} orders={s['orders']} {drivers_str}")
            for frame in s["sample_stack"][:8]:
                parts.append(f"      {frame}")

    return {
        "total_events": len(events),
        "order_distribution": dict(sorted(order_dist.items())),
        "task_counts": dict(task_counts.most_common(20)),
        "top_stacks": top_stacks[:50],
        "driver_hits": dict(driver_hits.most_common()),
        "summary": "\n".join(parts),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Trace high-order page allocations with kernel stacks during app interaction"
    )
    p.add_argument("--serial", required=True, help="Target device serial")
    p.add_argument(
        "--interaction",
        choices=["douyin", "camera", "both", "none"],
        default="douyin",
        help="Which interaction workload to run during tracing",
    )
    p.add_argument("--duration-s", type=int, default=20, help="Trace duration in seconds")
    p.add_argument("--min-order", type=int, default=2, help="Minimum page order to record (default: 2)")
    p.add_argument("--out-dir", default=None, help="Output directory (auto-generated if omitted)")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    ensure_adb_works()

    serial = args.serial

    # Output dir
    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_dir = Path(f"trace_{args.interaction}_{ts}")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {out_dir}")

    # Setup trace
    print("Setting up trace environment...")
    try:
        setup_trace(serial, min_order=args.min_order)
    except TraceSetupError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Install signal handler for cleanup
    stop_event = threading.Event()

    def _handle(_sig, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    try:
        # Start tracing
        print(f"Starting trace (order >= {args.min_order}, global stacktrace)...")
        start_tracing(serial)

        # Run interaction
        if args.interaction == "douyin":
            print("Running: interact_douyin (swipe through feed)...")
            result = interact_douyin(serial, swipes=max(1, args.duration_s // 3), gap_s=2.0)
            print(f"  Result: {json.dumps(result, ensure_ascii=False)}")
        elif args.interaction == "camera":
            print("Running: interact_camera (take photos)...")
            shots = max(1, args.duration_s // 4)
            result = interact_camera(serial, shots=shots, gap_s=1.5)
            print(f"  Result: {json.dumps(result, ensure_ascii=False)}")
        elif args.interaction == "both":
            print("Running: interact_camera + interact_douyin...")
            r1 = interact_camera(serial, shots=3, gap_s=1.5)
            print(f"  Camera: {json.dumps(r1, ensure_ascii=False)}")
            r2 = interact_douyin(serial, swipes=max(1, args.duration_s // 4), gap_s=2.0)
            print(f"  Douyin: {json.dumps(r2, ensure_ascii=False)}")
            result = {"camera": r1, "douyin": r2}
        else:
            print(f"Running: idle (no interaction, capturing background for {args.duration_s}s)...")
            time.sleep(args.duration_s)
            result = {"idle": True}

        # Small wait for trace buffer flush
        time.sleep(2)

        # Stop tracing
        print("Stopping trace...")
        stop_tracing(serial)

        # Read trace buffer
        print("Reading trace buffer...")
        raw = read_trace(serial)
        raw_path = out_dir / "raw_trace.txt"
        raw_path.write_text(raw, encoding="utf-8")
        print(f"  Raw trace: {raw_path} ({len(raw)} bytes)")

        # Parse and analyze
        print("Parsing and analyzing...")
        events = parse_trace(raw)
        print(f"  Parsed {len(events)} events")

        analysis = analyze(events)
        print()
        print(analysis["summary"])

        # Save
        (out_dir / "stacks.json").write_text(
            json.dumps(analysis["top_stacks"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out_dir / "analysis.txt").write_text(analysis["summary"], encoding="utf-8")

        # Also save raw analysis
        analysis_out = {k: v for k, v in analysis.items() if k != "top_stacks"}
        analysis_out["top_stack_count"] = len(analysis["top_stacks"])
        (out_dir / "analysis.json").write_text(
            json.dumps(analysis_out, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print(f"\nResults saved to {out_dir}/")

    finally:
        # Always clean up trace state
        print("Cleaning up trace environment...")
        teardown_trace(serial)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
