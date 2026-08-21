#!/usr/bin/env python3
"""Trace high-order allocation related direct reclaim and compaction stalls.

This host-side script configures ftrace events on an Android device, runs an
optional idle capture window, then parses the trace into per-stall and summary
artifacts. It intentionally does not require kernel changes.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import signal
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from utils.adb_utils import adb_shell_cp, ensure_adb_works, resolve_serial
from utils.alloc_reason import classify_stack, normalize_stack


TRACE_BASE = "/sys/kernel/tracing"
EVENTS = {
    "mm_page_alloc": f"{TRACE_BASE}/events/kmem/mm_page_alloc",
    "direct_reclaim_begin": f"{TRACE_BASE}/events/vmscan/mm_vmscan_direct_reclaim_begin",
    "direct_reclaim_end": f"{TRACE_BASE}/events/vmscan/mm_vmscan_direct_reclaim_end",
    "compaction_try": f"{TRACE_BASE}/events/compaction/mm_compaction_try_to_compact_pages",
    "compaction_begin": f"{TRACE_BASE}/events/compaction/mm_compaction_begin",
    "compaction_end": f"{TRACE_BASE}/events/compaction/mm_compaction_end",
    "page_cache_ra_order": f"{TRACE_BASE}/events/readahead/page_cache_ra_order",
}

HEADER_RE = re.compile(
    r"^\s*(?P<task>.+?)-(?P<pid>\d+)\s+"
    r"\[\d+\]\s+\S+\s+"
    r"(?P<ts>\d+\.\d+):\s+"
    r"(?P<event>[A-Za-z0-9_]+):\s+"
    r"(?P<body>.*)$"
)
STACK_RE = re.compile(r"^\s*=>\s+(?P<func>\S+)")


@dataclass
class TraceEvent:
    event: str
    task: str
    pid: int
    ts: float
    body: str
    fields: Dict[str, str]
    stack: List[str]


@dataclass
class StallEvent:
    kind: str
    reason: str
    order: int
    gfp: str
    task: str
    pid: int
    start_ts: float
    end_ts: float
    duration_ms: float
    detail: str


@dataclass
class TraceReport:
    events: List[TraceEvent]
    stalls: List[StallEvent]
    unmatched: Dict[str, int]


def parse_fields(body: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for m in re.finditer(r"([A-Za-z0-9_]+)=([^ ]+)", body):
        key = m.group(1)
        value = m.group(2).rstrip(",")
        fields[key] = value
    return fields


def parse_events(raw: str) -> List[TraceEvent]:
    events: List[TraceEvent] = []
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = HEADER_RE.match(line)
        if not m:
            i += 1
            continue

        stack: List[str] = []
        i += 1
        while i < len(lines):
            if HEADER_RE.match(lines[i]):
                break
            sm = STACK_RE.match(lines[i])
            if sm:
                stack.append(sm.group("func"))
            elif lines[i].strip().startswith("=>"):
                stack.append(lines[i].strip().removeprefix("=>").strip())
            i += 1

        events.append(
            TraceEvent(
                event=m.group("event"),
                task=m.group("task").strip(),
                pid=int(m.group("pid")),
                ts=float(m.group("ts")),
                body=m.group("body"),
                fields=parse_fields(m.group("body")),
                stack=normalize_stack(stack),
            )
        )
    return events


def _int_field(fields: Dict[str, str], key: str, default: int = -1) -> int:
    value = fields.get(key, "")
    try:
        return int(value, 0)
    except ValueError:
        return default


def _recent_reason(events: Sequence[TraceEvent], idx: int, pid: int, ts: float, window_s: float) -> str:
    for j in range(idx - 1, -1, -1):
        ev = events[j]
        if ts - ev.ts > window_s:
            break
        if ev.pid != pid:
            continue
        if ev.stack:
            reason = classify_stack(ev.stack)
            if reason != "unknown":
                return reason
    return "unknown"


def parse_trace(raw: str, *, reason_window_s: float = 0.25) -> TraceReport:
    events = parse_events(raw)
    direct_begins: Dict[Tuple[int, str], TraceEvent] = {}
    compact_try: Dict[Tuple[int, str], TraceEvent] = {}
    compact_begins: Dict[Tuple[int, str], TraceEvent] = {}
    stalls: List[StallEvent] = []
    unmatched = {
        "direct_reclaim_begin": 0,
        "direct_reclaim_end": 0,
        "compaction_try": 0,
        "compaction_begin": 0,
        "compaction_end": 0,
    }

    for idx, ev in enumerate(events):
        key = (ev.pid, ev.task)
        if ev.event == "mm_vmscan_direct_reclaim_begin":
            direct_begins[key] = ev
            continue
        if ev.event == "mm_vmscan_direct_reclaim_end":
            begin = direct_begins.pop(key, None)
            if not begin:
                unmatched["direct_reclaim_end"] += 1
                continue
            order = _int_field(begin.fields, "order")
            reason = _recent_reason(events, idx, ev.pid, begin.ts, reason_window_s)
            detail = "nr_reclaimed=" + ev.fields.get("nr_reclaimed", "")
            stalls.append(
                StallEvent(
                    kind="direct_reclaim",
                    reason=reason,
                    order=order,
                    gfp=begin.fields.get("gfp_flags", ""),
                    task=ev.task,
                    pid=ev.pid,
                    start_ts=begin.ts,
                    end_ts=ev.ts,
                    duration_ms=max(0.0, (ev.ts - begin.ts) * 1000.0),
                    detail=detail,
                )
            )
            continue
        if ev.event == "mm_compaction_try_to_compact_pages":
            compact_try[key] = ev
            continue
        if ev.event == "mm_compaction_begin":
            compact_begins[key] = ev
            continue
        if ev.event == "mm_compaction_end":
            begin = compact_begins.pop(key, None)
            if not begin:
                unmatched["compaction_end"] += 1
                continue
            try_ev = compact_try.get(key)
            order = _int_field(try_ev.fields if try_ev else {}, "order")
            reason_ts = try_ev.ts if try_ev else begin.ts
            reason = _recent_reason(events, idx, ev.pid, reason_ts, reason_window_s)
            detail_parts = []
            if try_ev:
                detail_parts.append("priority=" + try_ev.fields.get("priority", ""))
            detail_parts.append("mode=" + ev.fields.get("mode", begin.fields.get("mode", "")))
            detail_parts.append("status=" + ev.fields.get("status", ""))
            stalls.append(
                StallEvent(
                    kind="compaction",
                    reason=reason,
                    order=order,
                    gfp=(try_ev.fields.get("gfp_mask", "") if try_ev else ""),
                    task=ev.task,
                    pid=ev.pid,
                    start_ts=begin.ts,
                    end_ts=ev.ts,
                    duration_ms=max(0.0, (ev.ts - begin.ts) * 1000.0),
                    detail=" ".join(x for x in detail_parts if x and not x.endswith("=")),
                )
            )

    unmatched["direct_reclaim_begin"] = len(direct_begins)
    unmatched["compaction_try"] = len(compact_try)
    unmatched["compaction_begin"] = len(compact_begins)
    return TraceReport(events=events, stalls=stalls, unmatched=unmatched)


def summarize_stalls(stalls: Iterable[StallEvent]) -> List[Dict[str, object]]:
    buckets: Dict[Tuple[str, str, int, str], List[StallEvent]] = {}
    for stall in stalls:
        buckets.setdefault((stall.kind, stall.reason, stall.order, stall.gfp), []).append(stall)

    rows: List[Dict[str, object]] = []
    for (kind, reason, order, gfp), group in sorted(buckets.items(), key=lambda x: (-len(x[1]), x[0])):
        durations = sorted(s.duration_ms for s in group)
        rows.append(
            {
                "kind": kind,
                "reason": reason,
                "order": order,
                "gfp": gfp,
                "count": len(group),
                "total_ms": round(sum(durations), 3),
                "avg_ms": round(sum(durations) / len(durations), 3),
                "p50_ms": round(statistics.median(durations), 3),
                "p95_ms": round(durations[min(len(durations) - 1, int(len(durations) * 0.95))], 3),
                "max_ms": round(max(durations), 3),
            }
        )
    return rows


def _su(serial: str, cmd: str, *, timeout_s: int = 20, check: bool = False):
    from utils import adb_utils
    out = adb_utils.adb_shell_root(serial, cmd, timeout_s=timeout_s, check=check)
    return subprocess.CompletedProcess(["adb", "shell"], 0, stdout=out)


def _write(serial: str, path: str, value: str) -> None:
    cp = _su(serial, f"printf '%s' {value!r} > {path}", timeout_s=10)
    if cp.returncode != 0:
        raise RuntimeError(f"failed to write {path}: {cp.stderr or cp.stdout}")


def configure_trace(serial: str, *, min_order: int, buffer_kb: int) -> None:
    _write(serial, f"{TRACE_BASE}/tracing_on", "0")
    for event_dir in EVENTS.values():
        _write(serial, f"{event_dir}/enable", "0")
    _write(serial, f"{EVENTS['mm_page_alloc']}/filter", f"order >= {min_order}")
    _write(serial, f"{TRACE_BASE}/options/stacktrace", "1")
    _write(serial, f"{TRACE_BASE}/buffer_size_kb", str(buffer_kb))
    _write(serial, f"{TRACE_BASE}/trace", "")
    for event_dir in EVENTS.values():
        _write(serial, f"{event_dir}/enable", "1")


def cleanup_trace(serial: str) -> None:
    _write(serial, f"{TRACE_BASE}/tracing_on", "0")
    for event_dir in EVENTS.values():
        _write(serial, f"{event_dir}/enable", "0")
    _write(serial, f"{TRACE_BASE}/options/stacktrace", "0")
    _write(serial, f"{EVENTS['mm_page_alloc']}/filter", "0")


def read_trace(serial: str) -> str:
    cp = _su(serial, f"cat {TRACE_BASE}/trace", timeout_s=60)
    return cp.stdout or ""


def write_outputs(out_dir: Path, raw: str, report: TraceReport) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "raw_trace.txt").write_text(raw, encoding="utf-8")

    with (out_dir / "stall_events.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = [f.name for f in StallEvent.__dataclass_fields__.values()]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for stall in report.stalls:
            w.writerow(asdict(stall))

    summary = summarize_stalls(report.stalls)
    with (out_dir / "stall_summary_by_reason_order.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["kind", "reason", "order", "gfp", "count", "total_ms", "avg_ms", "p50_ms", "p95_ms", "max_ms"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in summary:
            w.writerow(row)

    md = ["# High-order Stall Summary\n"]
    md.append(f"- parsed events: {len(report.events)}\n")
    md.append(f"- stall events: {len(report.stalls)}\n")
    md.append(f"- unmatched: `{json.dumps(report.unmatched, sort_keys=True)}`\n")
    if summary:
        md.append("\n| kind | reason | order | count | total_ms | max_ms |\n")
        md.append("|---|---|---:|---:|---:|---:|\n")
        for row in summary[:30]:
            md.append(
                f"| {row['kind']} | {row['reason']} | {row['order']} | {row['count']} | "
                f"{row['total_ms']} | {row['max_ms']} |\n"
            )
    (out_dir / "stall_summary.md").write_text("".join(md), encoding="utf-8")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Trace high-order direct reclaim/compaction stalls")
    p.add_argument("--serial", default=None)
    p.add_argument("--duration-s", type=int, default=30)
    p.add_argument("--min-order", type=int, default=2)
    p.add_argument("--buffer-kb", type=int, default=16384)
    p.add_argument("--out-dir", default=None)
    p.add_argument("--parse-only", default=None, help="Parse an existing raw trace file instead of using adb")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir or f"highorder_stalls_{time.strftime('%Y%m%d_%H%M%S')}")

    if args.parse_only:
        raw = Path(args.parse_only).read_text(encoding="utf-8", errors="ignore")
        write_outputs(out_dir, raw, parse_trace(raw))
        print(f"Results saved to {out_dir}")
        return 0

    ensure_adb_works()
    serial = resolve_serial(args.serial)
    stop = False

    def _handle(_sig, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    try:
        configure_trace(serial, min_order=args.min_order, buffer_kb=args.buffer_kb)
        _write(serial, f"{TRACE_BASE}/tracing_on", "1")
        deadline = time.time() + max(0, args.duration_s)
        while time.time() < deadline and not stop:
            time.sleep(0.5)
        _write(serial, f"{TRACE_BASE}/tracing_on", "0")
        raw = read_trace(serial)
        write_outputs(out_dir, raw, parse_trace(raw))
        print(f"Results saved to {out_dir}")
        return 0
    finally:
        cleanup_trace(serial)


if __name__ == "__main__":
    raise SystemExit(main())
