#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Derive per-window deltas and fallback ratios from raw_samples.csv.

Main ratio:
  fallback_ratio = d_anon_fault_fallback / (d_anon_fault_alloc + d_anon_fault_fallback)

This script is intentionally dependency-light.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class Row:
    host_ts: int
    device_ts: Optional[int]
    error: str
    values: Dict[str, Optional[int]]


def _to_int(s: str) -> Optional[int]:
    s = (s or "").strip()
    if not s:
        return None
    return int(s) if s.isdigit() else None


def read_raw(path: Path) -> List[Row]:
    with path.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        counters = [c for c in r.fieldnames or [] if c not in ("host_ts", "device_ts", "error")]
        rows: List[Row] = []
        for d in r:
            host_ts = int(d.get("host_ts") or 0)
            dev_ts = _to_int(d.get("device_ts", ""))
            err = d.get("error", "") or ""
            vals: Dict[str, Optional[int]] = {c: _to_int(d.get(c, "")) for c in counters}
            rows.append(Row(host_ts=host_ts, device_ts=dev_ts, error=err, values=vals))
    return rows


def write_derived(raw: List[Row], out_csv: Path) -> None:
    # Determine counters from first row
    counters = sorted(list(raw[0].values.keys())) if raw else []

    # We'll compute deltas between consecutive samples where both values exist.
    fieldnames = [
        "window_end_host_ts",
        "window_s",
        "attempts",
        "d_anon_fault_alloc",
        "d_anon_fault_fallback",
        "fallback_ratio",
        "errors_in_window",
    ] + [f"d_{c}" for c in counters if c not in ("anon_fault_alloc", "anon_fault_fallback")]

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()

        prev = raw[0]
        prev_ts = prev.host_ts
        window_err = 1 if prev.error else 0

        for cur in raw[1:]:
            dt = max(1, cur.host_ts - prev_ts)
            # compute deltas
            deltas: Dict[str, Optional[int]] = {}
            for c in counters:
                a = prev.values.get(c)
                b = cur.values.get(c)
                if a is None or b is None:
                    deltas[c] = None
                else:
                    deltas[c] = b - a

            d_alloc = deltas.get("anon_fault_alloc")
            d_fallback = deltas.get("anon_fault_fallback")
            attempts = None
            ratio = None
            if d_alloc is not None and d_fallback is not None:
                attempts = d_alloc + d_fallback
                if attempts > 0:
                    ratio = d_fallback / attempts

            window_err += 1 if cur.error else 0

            row = {
                "window_end_host_ts": cur.host_ts,
                "window_s": dt,
                "attempts": attempts if attempts is not None else "",
                "d_anon_fault_alloc": d_alloc if d_alloc is not None else "",
                "d_anon_fault_fallback": d_fallback if d_fallback is not None else "",
                "fallback_ratio": f"{ratio:.6f}" if ratio is not None else "",
                "errors_in_window": window_err,
            }

            for c in counters:
                if c in ("anon_fault_alloc", "anon_fault_fallback"):
                    continue
                v = deltas.get(c)
                row[f"d_{c}"] = v if v is not None else ""

            w.writerow(row)

            # advance
            prev = cur
            prev_ts = cur.host_ts
            window_err = 0


# ---------- vmstat summary helpers ----------

VMSTAT_ALLOCSTALL_KEYS = ("allocstall_normal", "allocstall_movable")
VMSTAT_COMPACT_KEYS = ("compact_stall",)
VMSTAT_SWAPOUT_KEYS = ("pswpout", "zswpout", "swpout_zero")

VMA_ALLOC_SOURCES = (
    "anon_fault",
    "swapin",
    "cow",
    "file_cow",
    "uffd_copy",
    "uffd_zeropage",
    "ksm_copy",
    "fork_copy",
)
VMA_ALLOC_CLASSES = (
    "dalvik",
    "scudo",
    "bionic",
    "heap",
    "stack",
    "anon_shmem",
    "file_private",
    "other",
)
FILE_ALLOC_SOURCES = (
    "unknown",
    "readahead",
    "getfolio_mmap",
    "getfolio_write",
    "getfolio_other",
    "buffered_read",
    "read_cache",
    "erofs",
    "f2fs_compress",
    "btrfs",
    "fs_misc",
)


def read_vmstat_json(path: Path) -> Dict[str, int]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    result: Dict[str, int] = {}
    for k, v in data.items():
        try:
            result[k] = int(v)
        except (TypeError, ValueError):
            pass
    return result


def compute_vmstat_delta(start_path: Path, end_path: Path) -> Dict[str, Optional[int]]:
    """Return end - start for sampled vmstat metrics plus summary aggregates."""
    start = read_vmstat_json(start_path)
    end = read_vmstat_json(end_path)
    if not start or not end:
        return {}

    def _get(d: Dict[str, int], keys: tuple) -> int:
        return sum(d.get(k, 0) for k in keys)

    alloc_stall_start = _get(start, VMSTAT_ALLOCSTALL_KEYS)
    alloc_stall_end = _get(end, VMSTAT_ALLOCSTALL_KEYS)
    compact_stall_start = _get(start, VMSTAT_COMPACT_KEYS)
    compact_stall_end = _get(end, VMSTAT_COMPACT_KEYS)

    delta: Dict[str, Optional[int]] = {
        k: end.get(k, 0) - start.get(k, 0)
        for k in sorted(set(start) | set(end))
    }
    delta.update({
        "alloc_stall": alloc_stall_end - alloc_stall_start,
        "compact_stall": compact_stall_end - compact_stall_start,
        "pswpout": end.get("pswpout", 0) - start.get("pswpout", 0),
        "zswpout": end.get("zswpout", 0) - start.get("zswpout", 0),
        "swpout_zero": end.get("swpout_zero", 0) - start.get("swpout_zero", 0),
        "swapout_total_pages": _get(end, VMSTAT_SWAPOUT_KEYS) - _get(start, VMSTAT_SWAPOUT_KEYS),
        "thp_swpout": end.get("thp_swpout", 0) - start.get("thp_swpout", 0),
        "thp_swpout_fallback": end.get("thp_swpout_fallback", 0) - start.get("thp_swpout_fallback", 0),
    })
    return delta


def _delta(vmstat_delta: Dict[str, Optional[int]], key: str) -> int:
    value = vmstat_delta.get(key, 0)
    return int(value or 0)


def _order_pair(vmstat_delta: Dict[str, Optional[int]], prefix: str) -> tuple[int, int]:
    return _delta(vmstat_delta, f"{prefix}_order0"), _delta(vmstat_delta, f"{prefix}_order2")


def _folded_pages(order0: int, order2: int) -> int:
    return order0 + order2 * 4


def _ratio(num: int, den: int) -> str:
    if den <= 0:
        return "N/A"
    return f"{num / den:.6f}"


def _gib_from_pages(pages: int) -> str:
    return f"{pages * 4096 / (1024 ** 3):.2f}"


def _alloc_table_row(name: str, order0: int, order2: int, base_pages: Optional[int] = None) -> str:
    folios = order0 + order2
    pages = _folded_pages(order0, order2)
    base = pages if base_pages is None else base_pages
    return (
        f"| {name} | {order0} | {order2} | {folios} | {pages} | "
        f"{_gib_from_pages(pages)} | {_ratio(order2, folios)} | "
        f"{_ratio(order2 * 4, pages)} | {_ratio(pages, base)} |"
    )


def _append_alloc_breakdown(lines: List[str], title: str, rows: List[tuple[str, int, int]], base_pages: Optional[int] = None) -> None:
    lines.extend([
        f"## {title}",
        "| item | order0_folios | order2_folios | total_folios | folded_4k_pages | folded_GiB | order2_folio_ratio | order2_page_ratio | page_share |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for name, order0, order2 in rows:
        lines.append(_alloc_table_row(name, order0, order2, base_pages))
    lines.append("")


def write_summary(derived_csv: Path, out_md: Path,
                  vmstat_delta: Optional[Dict[str, int]] = None) -> None:
    alloc_total = 0
    fallback_total = 0
    attempts_total = 0
    order2_swpout_folios = 0
    order2_zswpout_folios = 0
    order2_swpout_fallback = 0

    with derived_csv.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for d in r:
            a = (d.get("d_anon_fault_alloc") or "").strip()
            fb = (d.get("d_anon_fault_fallback") or "").strip()
            at = (d.get("attempts") or "").strip()
            if a.lstrip("-").isdigit():
                alloc_total += int(a)
            if fb.lstrip("-").isdigit():
                fallback_total += int(fb)
            if at.lstrip("-").isdigit():
                attempts_total += int(at)
            swpout = (d.get("d_swpout") or "").strip()
            zswpout = (d.get("d_zswpout") or "").strip()
            swpout_fallback = (d.get("d_swpout_fallback") or "").strip()
            if swpout.lstrip("-").isdigit():
                order2_swpout_folios += int(swpout)
            if zswpout.lstrip("-").isdigit():
                order2_zswpout_folios += int(zswpout)
            if swpout_fallback.lstrip("-").isdigit():
                order2_swpout_fallback += int(swpout_fallback)

    overall_ratio = (fallback_total / attempts_total) if attempts_total > 0 else None

    vmstat_delta = vmstat_delta or {}
    alloc_stall = vmstat_delta.get("alloc_stall")
    compact_stall = vmstat_delta.get("compact_stall")
    swapout_total_pages = vmstat_delta.get("swapout_total_pages")
    pswpout = vmstat_delta.get("pswpout")
    zswpout = vmstat_delta.get("zswpout")
    swpout_zero = vmstat_delta.get("swpout_zero")
    thp_swpout = vmstat_delta.get("thp_swpout")
    thp_swpout_fallback = vmstat_delta.get("thp_swpout_fallback")

    def _fmt(v: Optional[int]) -> str:
        if v is None:
            return "N/A"
        return str(v)

    ratio_str = f"{overall_ratio:.6f}" if overall_ratio is not None else "N/A"

    order2_swapout_folios = order2_swpout_folios + order2_zswpout_folios
    order2_swapout_pages = order2_swapout_folios * 4
    order0_swapout_pages_est = None
    order2_swapout_page_ratio = None
    if swapout_total_pages is not None:
        order0_swapout_pages_est = max(0, swapout_total_pages - order2_swapout_pages)
        if swapout_total_pages > 0:
            order2_swapout_page_ratio = order2_swapout_pages / swapout_total_pages

    global_order0 = _delta(vmstat_delta, "alloc_success_order0")
    global_order2 = _delta(vmstat_delta, "alloc_success_order2")
    global_pages = _folded_pages(global_order0, global_order2)
    vma_order0 = _delta(vmstat_delta, "vma_anon_alloc_order0_total")
    vma_order2 = _delta(vmstat_delta, "vma_anon_alloc_order2_total")
    file_order0 = _delta(vmstat_delta, "file_alloc_order0_total")
    file_order2 = _delta(vmstat_delta, "file_alloc_order2_total")
    vma_pages = _folded_pages(vma_order0, vma_order2)
    file_pages = _folded_pages(file_order0, file_order2)
    unknown_pages = max(0, global_pages - vma_pages - file_pages)

    vma_source_rows = [
        (name, _delta(vmstat_delta, f"vma_anon_alloc_order0_{name}"),
         _delta(vmstat_delta, f"vma_anon_alloc_order2_{name}"))
        for name in VMA_ALLOC_SOURCES
    ]
    vma_class_rows = [
        (name, _delta(vmstat_delta, f"vma_anon_alloc_order0_class_{name}"),
         _delta(vmstat_delta, f"vma_anon_alloc_order2_class_{name}"))
        for name in VMA_ALLOC_CLASSES
    ]
    file_source_rows = [
        (name, _delta(vmstat_delta, f"file_alloc_order0_{name}"),
         _delta(vmstat_delta, f"file_alloc_order2_{name}"))
        for name in FILE_ALLOC_SOURCES
    ]

    vma_source_pages = sum(_folded_pages(o0, o2) for _, o0, o2 in vma_source_rows)
    vma_class_pages = sum(_folded_pages(o0, o2) for _, o0, o2 in vma_class_rows)
    file_source_pages = sum(_folded_pages(o0, o2) for _, o0, o2 in file_source_rows)

    lines: List[str] = [
        "# THP 16KB Anon Fallback Summary\n",
        "## General metrics (end - start)\n",
        "| metric | value |",
        "|--------|-------|",
        f"| anon_alloc | {alloc_total} |",
        f"| anon_fallback | {fallback_total} |",
        f"| fallback_ratio | {ratio_str} |",
        f"| alloc_stall | {_fmt(alloc_stall)} |",
        f"| compact_stall | {_fmt(compact_stall)} |",
        f"| alloc_success_order0 | {global_order0} |",
        f"| alloc_success_order2 | {global_order2} |",
        f"| alloc_success_folded_4k_pages | {global_pages} |",
        f"| alloc_success_folded_GiB | {_gib_from_pages(global_pages)} |",
        f"| vma_anon_alloc_folded_4k_pages | {vma_pages} |",
        f"| file_alloc_folded_4k_pages | {file_pages} |",
        f"| unclassified_alloc_folded_4k_pages | {unknown_pages} |",
        f"| vma_source_reconcile_pages | {vma_source_pages - vma_pages} |",
        f"| vma_class_reconcile_pages | {vma_class_pages - vma_pages} |",
        f"| file_source_reconcile_pages | {file_source_pages - file_pages} |",
        "",
        f"| pswpout_pages | {_fmt(pswpout)} |",
        f"| zswpout_pages | {_fmt(zswpout)} |",
        f"| swpout_zero_pages | {_fmt(swpout_zero)} |",
        f"| swapout_total_pages | {_fmt(swapout_total_pages)} |",
        f"| order2_16k_swpout_folios | {order2_swpout_folios} |",
        f"| order2_16k_zswpout_folios | {order2_zswpout_folios} |",
        f"| order2_16k_swpout_fallback | {order2_swpout_fallback} |",
        f"| order2_16k_swapout_pages_est | {order2_swapout_pages} |",
        f"| order0_swapout_pages_est | {_fmt(order0_swapout_pages_est)} |",
        f"| order2_16k_swapout_page_ratio | {order2_swapout_page_ratio:.6f} |" if order2_swapout_page_ratio is not None else "| order2_16k_swapout_page_ratio | N/A |",
        f"| thp_swpout_pmd_folios | {_fmt(thp_swpout)} |",
        f"| thp_swpout_fallback_pmd_folios | {_fmt(thp_swpout_fallback)} |",
        "",
        "- **anon_alloc**: total `anon_fault_alloc` during the experiment (THP stats end - start).",
        "- **anon_fallback**: total `anon_fault_fallback` during the experiment (THP stats end - start).",
        "- **fallback_ratio**: `anon_fallback / (anon_alloc + anon_fallback)`.",
        "- **alloc_stall**: `allocstall_normal + allocstall_movable` from `/proc/vmstat` (end - start).",
        "- **compact_stall**: `compact_stall` from `/proc/vmstat` (end - start).",
        "- **folded_4k_pages**: order-0 folios count as 1 page and order-2 folios count as 4 pages.",
        "- **source_reconcile_pages**: source subtotal minus total; expected value is 0 when every counted source is covered.",
        "- **order2_16k_swapout_pages_est**: `4 * (d_swpout + d_zswpout)` from the active 16KB mTHP stats directory.",
        "- **order0_swapout_pages_est**: `swapout_total_pages - order2_16k_swapout_pages_est`; this estimate assumes only 16KB mTHP is enabled and larger mTHP sizes are disabled.",
        "- **order2_16k_swapout_page_ratio**: estimated 16KB mTHP swapout page share among `pswpout + zswpout + swpout_zero`.",
        "",
        "Per-window deltas are available in `derived.csv` and `vmstat_derived.csv`.",
    ]

    lines.extend([
        "",
        "## Allocation Stall/Fail Source Matrix",
        "| event | anon_watermark | file_watermark | anon_fragment | file_fragment | anon_share | file_share |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    fail_anon = _delta(vmstat_delta, "alloc_fail_wmark_anon") + _delta(vmstat_delta, "alloc_fail_fragment_anon")
    fail_file = _delta(vmstat_delta, "alloc_fail_wmark_file") + _delta(vmstat_delta, "alloc_fail_fragment_file")
    stall_anon = _delta(vmstat_delta, "alloc_stall_wmark_anon") + _delta(vmstat_delta, "alloc_stall_fragment_anon")
    stall_file = _delta(vmstat_delta, "alloc_stall_wmark_file") + _delta(vmstat_delta, "alloc_stall_fragment_file")
    lines.extend([
        f"| alloc_fail | {_delta(vmstat_delta, 'alloc_fail_wmark_anon')} | {_delta(vmstat_delta, 'alloc_fail_wmark_file')} | {_delta(vmstat_delta, 'alloc_fail_fragment_anon')} | {_delta(vmstat_delta, 'alloc_fail_fragment_file')} | {_ratio(fail_anon, fail_anon + fail_file)} | {_ratio(fail_file, fail_anon + fail_file)} |",
        f"| alloc_stall | {_delta(vmstat_delta, 'alloc_stall_wmark_anon')} | {_delta(vmstat_delta, 'alloc_stall_wmark_file')} | {_delta(vmstat_delta, 'alloc_stall_fragment_anon')} | {_delta(vmstat_delta, 'alloc_stall_fragment_file')} | {_ratio(stall_anon, stall_anon + stall_file)} | {_ratio(stall_file, stall_anon + stall_file)} |",
        "",
    ])

    _append_alloc_breakdown(lines, "Global Allocation Split", [
        ("global", global_order0, global_order2),
        ("vma_anon", vma_order0, vma_order2),
        ("file", file_order0, file_order2),
        ("unclassified", unknown_pages, 0),
    ], global_pages)
    _append_alloc_breakdown(lines, "VMA Anonymous Source Breakdown", vma_source_rows, vma_pages)
    _append_alloc_breakdown(lines, "VMA Anonymous Class Breakdown", vma_class_rows, vma_pages)
    _append_alloc_breakdown(lines, "File Allocation Source Breakdown", file_source_rows, file_pages)

    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="derive deltas and fallback ratios from raw_samples.csv")
    p.add_argument("raw_csv", help="Path to raw_samples.csv")
    p.add_argument("--out-dir", default=None, help="Output dir (default: same dir as raw)")
    p.add_argument("--vmstat-start", default=None, help="Path to vmstat_start.json")
    p.add_argument("--vmstat-end", default=None, help="Path to vmstat_end.json")

    args = p.parse_args(argv)

    raw_path = Path(args.raw_csv)
    if not raw_path.exists():
        raise FileNotFoundError(str(raw_path))

    out_dir = Path(args.out_dir) if args.out_dir else raw_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = read_raw(raw_path)
    if len(raw) < 2:
        (out_dir / "summary.md").write_text("# THP 16KB Anon Fallback Summary\n\nNot enough samples.\n", encoding="utf-8")
        return 0

    derived_csv = out_dir / "derived.csv"
    write_derived(raw, derived_csv)

    vmstat_delta = None
    if args.vmstat_start and args.vmstat_end:
        vmstat_delta = compute_vmstat_delta(Path(args.vmstat_start), Path(args.vmstat_end))

    summary_md = out_dir / "summary.md"
    write_summary(derived_csv, summary_md, vmstat_delta)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
