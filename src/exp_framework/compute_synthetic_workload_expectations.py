#!/usr/bin/env python3
"""Compute expected synthetic MTHP workload pressure from profiles + runtime scales.

The synthetic APK workload records its build-time geometry in profiles.tsv and
receives runtime scale knobs through the memstress run manifest.  This script
replays the same scale rules used by WorkloadRuntime.java and summarizes the
expected per-app pressure before comparing it with kernel counters.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path
from statistics import median
from typing import Any


def java_round(value: float) -> int:
    if value >= 0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def scaled_count(old_count: int, scale: float) -> int:
    if old_count <= 0 or scale == 0.0:
        return 0
    return max(1, java_round(old_count * scale))


def as_int(row: dict[str, str], key: str, default: int = 0) -> int:
    value = row.get(key, "")
    if value is None or value == "":
        return default
    return int(float(value))


def as_float(value: Any, default: float = 1.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_profiles(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    return {row["package"]: row for row in rows}


def load_manifest_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("config", {}).get("memstress", {})


def load_packages(package_file: Path | None, manifest_memstress: dict[str, Any], profiles: dict[str, dict[str, str]]) -> list[str]:
    if package_file is not None:
        packages = []
        for line in package_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                packages.append(line.split()[0])
        return packages
    manifest_packages = manifest_memstress.get("packages")
    if isinstance(manifest_packages, list):
        return [str(pkg) for pkg in manifest_packages]
    return sorted(profiles)


def load_cycle_launch_counts(path: Path | None) -> Counter[str]:
    counts: Counter[str] = Counter()
    if path is None or not path.exists():
        return counts
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            for launched in event.get("launched", []) or []:
                package = str(launched).split("/", 1)[0]
                if package:
                    counts[package] += 1
    return counts


def profile_family(name: str) -> str:
    for prefix in ("java", "scudo", "dlopen", "vma_cow", "vma", "cow_burst", "cow", "mixed", "file_bg", "monster"):
        if name == prefix or name.startswith(prefix + "_"):
            return prefix
    return name.split("_", 1)[0]


def apply_runtime_scales(row: dict[str, str], scales: dict[str, float]) -> dict[str, int]:
    vma_count = as_int(row, "vma_count")
    touch_pages_per_vma = as_int(row, "touch_pages_per_vma", max(1, as_int(row, "vma_size_kb", 64) // 4))
    cow_pages_per_child = as_int(row, "cow_pages_per_child")
    filemap_file_mb = as_int(row, "filemap_file_mb")
    dlopen_lib_count = as_int(row, "dlopen_lib_count")

    vma_count = scaled_count(vma_count, scales["vma_count"])

    scaled_pages = int(touch_pages_per_vma * scales["anon_vma_size"])
    scaled_pages = max(4, (scaled_pages // 4) * 4)
    touch_pages_per_vma = scaled_pages
    vma_size_kb = touch_pages_per_vma * 4

    scaled_cow = max(0, java_round(cow_pages_per_child * scales["cow_pages"]))
    resident_pages = max(0, vma_count) * max(1, touch_pages_per_vma)
    cow_pages_per_child = min(scaled_cow, resident_pages)

    if filemap_file_mb > 0:
        filemap_file_mb = max(1, java_round(filemap_file_mb * scales["filemap_size"]))
    else:
        filemap_file_mb = 0

    dlopen_lib_count = scaled_count(dlopen_lib_count, scales["dlopen_lib_count"])

    return {
        "vma_count": vma_count,
        "touch_pages_per_vma": touch_pages_per_vma,
        "vma_size_kb": vma_size_kb,
        "cow_pages_per_child": cow_pages_per_child,
        "filemap_file_mb": filemap_file_mb,
        "dlopen_lib_count": dlopen_lib_count,
    }


def compute_row(row: dict[str, str], scales: dict[str, float], base_page_kb: int, pad_rodata_kb: int) -> dict[str, Any]:
    scaled = apply_runtime_scales(row, scales)
    process_count = max(1, min(4, as_int(row, "process_count", 1)))
    main_vmas = scaled["vma_count"]
    worker_vmas = max(32, main_vmas // 3) if process_count > 1 and main_vmas > 0 else 0
    total_vmas = main_vmas + max(0, process_count - 1) * worker_vmas
    pages_per_vma = scaled["touch_pages_per_vma"]
    anon_pages = total_vmas * pages_per_vma

    fork_children = as_int(row, "fork_children")
    main_cow_pages = scaled["cow_pages_per_child"]
    worker_cow_pages = main_cow_pages // 3 if process_count > 1 else 0
    cow_pages = fork_children * (main_cow_pages + max(0, process_count - 1) * worker_cow_pages)

    dlopen_per_process = scaled["dlopen_lib_count"]
    dlopen_invocations = dlopen_per_process * process_count
    dlopen_rodata_pages = dlopen_invocations * math.ceil(pad_rodata_kb / base_page_kb)

    filemap_threads = as_int(row, "filemap_threads")
    filemap_file_mb = scaled["filemap_file_mb"]
    filemap_pages = process_count * filemap_threads * filemap_file_mb * 1024 // base_page_kb

    scudo_main_mb = as_int(row, "scudo_live_mb")
    scudo_threads = max(1, as_int(row, "scudo_threads", 1))
    scudo_worker_threads = max(1, scudo_threads // 2)
    scudo_worker_mb = 0.0
    if process_count > 1:
        # Native code starts fewer Scudo worker threads in secondary processes,
        # but each worker still divides its target by the original cfg.scudo_threads.
        scudo_worker_mb = (scudo_main_mb // 3) * scudo_worker_threads / scudo_threads
    scudo_total_mb = scudo_main_mb + max(0, process_count - 1) * scudo_worker_mb
    scudo_pages = scudo_total_mb * 1024 // base_page_kb

    java_main_mb = as_int(row, "java_live_mb")
    java_worker_mb = java_main_mb // 3 if process_count > 1 else 0
    java_total_mb = java_main_mb + max(0, process_count - 1) * java_worker_mb
    java_pages = java_total_mb * 1024 // base_page_kb

    total_pages = anon_pages + cow_pages + dlopen_rodata_pages + filemap_pages + scudo_pages + java_pages

    return {
        "package": row["package"],
        "profile_index": as_int(row, "profile_index"),
        "profile_name": row.get("profile_name", ""),
        "profile_family": profile_family(row.get("profile_name", "")),
        "process_count": process_count,
        "vma_count_main": main_vmas,
        "vma_count_worker_each": worker_vmas,
        "anon_vmas_total": total_vmas,
        "vma_size_kb": scaled["vma_size_kb"],
        "pages_per_vma_4k": pages_per_vma,
        "anon_full_fault_pages_4k": anon_pages,
        "anon_full_fault_mib": anon_pages * base_page_kb / 1024,
        "fork_children": fork_children,
        "cow_pages_per_child_main_4k": main_cow_pages,
        "cow_pages_per_child_worker_4k": worker_cow_pages,
        "cow_fault_pages_4k": cow_pages,
        "cow_fault_mib": cow_pages * base_page_kb / 1024,
        "dlopen_lib_count_per_process": dlopen_per_process,
        "dlopen_invocations_total": dlopen_invocations,
        "dlopen_rodata_pages_4k": dlopen_rodata_pages,
        "dlopen_rodata_mib": dlopen_rodata_pages * base_page_kb / 1024,
        "filemap_threads": filemap_threads,
        "filemap_file_mb_per_thread": filemap_file_mb,
        "filemap_read_pages_4k": filemap_pages,
        "filemap_read_mib": filemap_pages * base_page_kb / 1024,
        "scudo_threads_main": scudo_threads,
        "scudo_threads_worker_each": scudo_worker_threads if process_count > 1 else 0,
        "scudo_live_mib_requested": scudo_total_mb,
        "scudo_live_pages_4k_requested": scudo_pages,
        "java_live_mib_requested": java_total_mb,
        "java_live_pages_4k_requested": java_pages,
        "expected_component_pages_4k": total_pages,
        "expected_component_mib": total_pages * base_page_kb / 1024,
        "apk_bytes": as_int(row, "apk_bytes"),
    }


def sum_key(rows: list[dict[str, Any]], key: str) -> float:
    return sum(float(row.get(key, 0) or 0) for row in rows)


def pct(value: float, total: float) -> float:
    return 0.0 if total <= 0 else value * 100.0 / total


def describe(values: list[float]) -> str:
    if not values:
        return "0 / 0 / 0"
    return f"{min(values):,.0f} / {median(values):,.0f} / {max(values):,.0f}"


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_pages = sum_key(rows, "expected_component_pages_4k")
    components = {
        "anon_full_fault": sum_key(rows, "anon_full_fault_pages_4k"),
        "cow_fault": sum_key(rows, "cow_fault_pages_4k"),
        "dlopen_rodata_read": sum_key(rows, "dlopen_rodata_pages_4k"),
        "filemap_read": sum_key(rows, "filemap_read_pages_4k"),
        "scudo_live_requested": sum_key(rows, "scudo_live_pages_4k_requested"),
        "java_live_requested": sum_key(rows, "java_live_pages_4k_requested"),
    }
    by_family: dict[str, dict[str, float]] = {}
    for row in rows:
        family = str(row["profile_family"])
        bucket = by_family.setdefault(family, {"apps": 0, "pages": 0, "anon": 0, "cow": 0, "filemap": 0, "dlopen": 0})
        bucket["apps"] += 1
        bucket["pages"] += float(row["expected_component_pages_4k"])
        bucket["anon"] += float(row["anon_full_fault_pages_4k"])
        bucket["cow"] += float(row["cow_fault_pages_4k"])
        bucket["filemap"] += float(row["filemap_read_pages_4k"])
        bucket["dlopen"] += float(row["dlopen_rodata_pages_4k"])
    return {
        "apps": len(rows),
        "total_pages_4k": total_pages,
        "total_mib": total_pages * 4 / 1024,
        "components": components,
        "families": by_family,
    }


def multiply_for_launches(rows: list[dict[str, Any]], launch_counts: Counter[str]) -> list[dict[str, Any]]:
    weighted = []
    numeric_fields = {
        "anon_full_fault_pages_4k",
        "anon_full_fault_mib",
        "cow_fault_pages_4k",
        "cow_fault_mib",
        "dlopen_invocations_total",
        "dlopen_rodata_pages_4k",
        "dlopen_rodata_mib",
        "filemap_read_pages_4k",
        "filemap_read_mib",
        "scudo_live_mib_requested",
        "scudo_live_pages_4k_requested",
        "java_live_mib_requested",
        "java_live_pages_4k_requested",
        "expected_component_pages_4k",
        "expected_component_mib",
    }
    for row in rows:
        launches = int(launch_counts.get(str(row["package"]), 0))
        out = dict(row)
        out["launch_count"] = launches
        for key in numeric_fields:
            out[key] = float(out.get(key, 0) or 0) * launches
        weighted.append(out)
    return weighted


def component_table_lines(summary: dict[str, Any]) -> list[str]:
    total_pages = float(summary["total_pages_4k"])
    components = summary["components"]
    labels = {
        "anon_full_fault": "anonymous VMA full write-fault",
        "cow_fault": "fork/COW write-fault target",
        "dlopen_rodata_read": "dlopen pad `.so` rodata read",
        "filemap_read": "explicit filemap read",
        "scudo_live_requested": "Scudo native heap requested live set",
        "java_live_requested": "Java heap requested live set",
    }
    lines = [
        "| component | pages | MiB | share |",
        "|---|---:|---:|---:|",
    ]
    for key, label in labels.items():
        value = float(components[key])
        lines.append(f"| {label} | {value:,.0f} | {value * 4 / 1024:,.1f} | {pct(value, total_pages):.1f}% |")
    return lines


def write_markdown(path: Path, rows: list[dict[str, Any]], summary: dict[str, Any], scales: dict[str, float], package_file: Path | None, launch_summary: dict[str, Any] | None = None) -> None:
    total_pages = float(summary["total_pages_4k"])
    components = summary["components"]
    lines = [
        "# Synthetic Workload Expectation",
        "",
        "This file is computed from `profiles.tsv` plus the runtime scale values recorded in `run_manifest.json`. It is a workload expectation, not a kernel measurement.",
        "All page counts are expressed as 4 KiB base pages so they can be compared with vmstat and order-2 accounting.",
        "",
        "## Runtime Scales",
        "",
        "| scale | value | meaning |",
        "|---|---:|---|",
        f"| VMA count | {scales['vma_count']:.4g} | Multiplies how many named anonymous VMAs each profile creates. |",
        f"| anonymous VMA size | {scales['anon_vma_size']:.4g} | Multiplies pages per anonymous VMA, rounded down to a multiple of 4 pages with a minimum of 4 pages. |",
        f"| COW pages | {scales['cow_pages']:.4g} | Multiplies pages each fork child writes, capped by resident anonymous pages. |",
        f"| filemap size | {scales['filemap_size']:.4g} | Multiplies explicit file-backed mmap file size. |",
        f"| dlopen library count | {scales['dlopen_lib_count']:.4g} | Multiplies the number of pad shared libraries dlopened per process. |",
        "",
        "## Aggregate Expected Pressure",
        "",
        f"- Packages included: {summary['apps']}",
        f"- Package file: `{package_file}`" if package_file else "- Package file: not supplied; used manifest package order or all profiles.",
        f"- Total expected component pressure: {total_pages:,.0f} 4 KiB pages ({float(summary['total_mib']):,.1f} MiB).",
        "- Components are intentionally separated because they stress different kernel paths: anonymous VMA fault, fork/COW write fault, file-backed reads through dlopen/filemap, Scudo native heap, and Java heap.",
        "",
    ]
    lines += component_table_lines(summary)

    if launch_summary is not None:
        launch_pages = float(launch_summary["total_pages_4k"])
        lines += [
            "",
            "## Observed Launch-Weighted Expected Pressure",
            "",
            "This section multiplies each app's expected pressure by its actual launch count in `memstress/cycle_log.jsonl`. It is an upper-bound launch-weighted request estimate: if an app is already alive, WorkloadRuntime does not allocate the native/Java live sets a second time.",
            f"- Total launches counted: {int(launch_summary.get('launches', 0))}",
            f"- Launch-weighted expected component pressure: {launch_pages:,.0f} 4 KiB pages ({launch_pages * 4 / 1024:,.1f} MiB).",
            "",
        ]
        lines += component_table_lines(launch_summary)

    lines += [
        "",
        "## Per-App Distribution",
        "",
        "The min / median / max values below describe the 60 installed synthetic apps after runtime scaling.",
        "",
        "| metric | min / median / max |",
        "|---|---:|",
        f"| anonymous VMA count per app | {describe([float(r['anon_vmas_total']) for r in rows])} |",
        f"| anonymous full-fault pages per app | {describe([float(r['anon_full_fault_pages_4k']) for r in rows])} |",
        f"| COW target pages per app | {describe([float(r['cow_fault_pages_4k']) for r in rows])} |",
        f"| dlopen invocations per app | {describe([float(r['dlopen_invocations_total']) for r in rows])} |",
        f"| explicit filemap read pages per app | {describe([float(r['filemap_read_pages_4k']) for r in rows])} |",
        "",
        "## Profile Families",
        "",
        "| family | apps | total pages | share | anon pages | COW pages | filemap pages | dlopen rodata pages |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for family, data in sorted(summary["families"].items(), key=lambda kv: (-kv[1]["pages"], kv[0])):
        pages = float(data["pages"])
        lines.append(
            f"| {family} | {int(data['apps'])} | {pages:,.0f} | {pct(pages, total_pages):.1f}% | "
            f"{data['anon']:,.0f} | {data['cow']:,.0f} | {data['filemap']:,.0f} | {data['dlopen']:,.0f} |"
        )

    lines += [
        "",
        "## Per-App Table Location",
        "",
        "The full per-app calculation is in `synthetic_workload_expectations.tsv`. Key fields include `anon_vmas_total`, `anon_full_fault_pages_4k`, `cow_fault_pages_4k`, `dlopen_invocations_total`, `filemap_read_pages_4k`, `scudo_live_pages_4k_requested`, and `java_live_pages_4k_requested`.",
        "If `memstress/cycle_log.jsonl` was available, launch-weighted per-app totals are also written to `synthetic_workload_launch_weighted.tsv`.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-tsv", required=True, type=Path)
    parser.add_argument("--run-manifest", type=Path)
    parser.add_argument("--package-file", type=Path)
    parser.add_argument("--cycle-log", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--base-page-kb", type=int, default=4)
    parser.add_argument("--pad-rodata-kb", type=int, default=256)
    parser.add_argument("--vma-count-scale", type=float)
    parser.add_argument("--anon-vma-size-scale", type=float)
    parser.add_argument("--cow-pages-scale", type=float)
    parser.add_argument("--filemap-size-scale", type=float)
    parser.add_argument("--dlopen-lib-count-scale", type=float)
    args = parser.parse_args()

    manifest_memstress = load_manifest_config(args.run_manifest)
    scales = {
        "vma_count": as_float(args.vma_count_scale, as_float(manifest_memstress.get("synthetic_vma_count_scale"), 1.0)),
        "anon_vma_size": as_float(args.anon_vma_size_scale, as_float(manifest_memstress.get("synthetic_anon_vma_size_scale"), 1.0)),
        "cow_pages": as_float(args.cow_pages_scale, as_float(manifest_memstress.get("synthetic_cow_pages_scale"), 1.0)),
        "filemap_size": as_float(args.filemap_size_scale, as_float(manifest_memstress.get("synthetic_filemap_size_scale"), 1.0)),
        "dlopen_lib_count": as_float(args.dlopen_lib_count_scale, as_float(manifest_memstress.get("synthetic_dlopen_lib_count_scale"), 1.0)),
    }

    profiles = load_profiles(args.profiles_tsv)
    packages = load_packages(args.package_file, manifest_memstress, profiles)
    missing = [pkg for pkg in packages if pkg not in profiles]
    if missing:
        raise SystemExit(f"packages missing from profiles.tsv: {', '.join(missing[:10])}")
    rows = [compute_row(profiles[pkg], scales, args.base_page_kb, args.pad_rodata_kb) for pkg in packages]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_tsv(args.out_dir / "synthetic_workload_expectations.tsv", rows)
    summary = aggregate(rows)
    launch_counts = load_cycle_launch_counts(args.cycle_log)
    launch_summary = None
    weighted_rows: list[dict[str, Any]] = []
    if launch_counts:
        weighted_rows = multiply_for_launches(rows, launch_counts)
        write_tsv(args.out_dir / "synthetic_workload_launch_weighted.tsv", weighted_rows)
        launch_summary = aggregate(weighted_rows)
        launch_summary["launches"] = sum(launch_counts.values())
    payload = {
        "scales": scales,
        "base_page_kb": args.base_page_kb,
        "pad_rodata_kb": args.pad_rodata_kb,
        "summary": summary,
        "launch_weighted_summary": launch_summary,
        "apps": rows,
    }
    (args.out_dir / "synthetic_workload_expectations.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_markdown(args.out_dir / "synthetic_workload_expectations.md", rows, summary, scales, args.package_file, launch_summary)
    print(args.out_dir / "synthetic_workload_expectations.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
