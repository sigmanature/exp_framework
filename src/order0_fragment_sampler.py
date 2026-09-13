#!/usr/bin/env python3
"""Collect full order-0 provenance and order-2 fragmentation snapshots."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ZONE_HEADER = re.compile(r"^Node\s+(\d+),\s+zone\s+(\S+)")
PAGETYPE_LINE = re.compile(
    r"^Node\s+(\d+),\s+zone\s+(\S+),\s+type\s+(\S+)\s+(.*)$"
)
CPU_LINE = re.compile(r"^\s+cpu:\s+(\d+)")
ORDER0_LINE = re.compile(r"^\s+order0:\s+(\d+)")
PRESENT_LINE = re.compile(r"^\s+present\s+(\d+)")
MANAGED_LINE = re.compile(r"^\s+managed\s+(\d+)")


class SamplerState:
    def __init__(self) -> None:
        self.stop_requested = False

    def request_stop(self, _signum: int, _frame: Any) -> None:
        self.stop_requested = True


def run_adb(adb: str, serial: str, command: str, timeout_s: int) -> Tuple[int, str, str]:
    try:
        completed = subprocess.run(
            [adb, "-s", serial, "shell", command],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 124, "", str(error)
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def parse_vmstat(raw: str) -> Dict[str, int]:
    values: Dict[str, int] = {}
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            values[fields[0]] = int(fields[1])
        except ValueError:
            continue
    return values


def parse_zoneinfo_pcp(raw: str) -> Dict[str, Any]:
    current_zone: Optional[Tuple[int, str]] = None
    current_cpu: Optional[int] = None
    pcp_total = 0
    pcp_by_zone: Dict[str, int] = {}
    pcp_by_node: Dict[str, int] = {}
    present_pages: Dict[str, int] = {}
    managed_pages: Dict[str, int] = {}

    for line in raw.splitlines():
        header_match = ZONE_HEADER.match(line)
        if header_match:
            node_id = int(header_match.group(1))
            zone_name = header_match.group(2)
            current_zone = (node_id, zone_name)
            current_cpu = None
            continue
        if current_zone is None:
            continue
        cpu_match = CPU_LINE.match(line)
        if cpu_match:
            current_cpu = int(cpu_match.group(1))
            continue
        order0_match = ORDER0_LINE.match(line)
        if order0_match and current_cpu is not None:
            node_id, zone_name = current_zone
            order0_pages = int(order0_match.group(1))
            zone_key = f"node{node_id}_{zone_name}"
            node_key = f"node{node_id}"
            pcp_total += order0_pages
            pcp_by_zone[zone_key] = pcp_by_zone.get(zone_key, 0) + order0_pages
            pcp_by_node[node_key] = pcp_by_node.get(node_key, 0) + order0_pages
            continue
        present_match = PRESENT_LINE.match(line)
        if present_match:
            node_id, zone_name = current_zone
            present_pages[f"node{node_id}_{zone_name}"] = int(present_match.group(1))
            continue
        managed_match = MANAGED_LINE.match(line)
        if managed_match:
            node_id, zone_name = current_zone
            managed_pages[f"node{node_id}_{zone_name}"] = int(managed_match.group(1))

    return {
        "pcp_order0_zoneinfo_total": pcp_total,
        "pcp_order0_zoneinfo_by_zone": pcp_by_zone,
        "pcp_order0_zoneinfo_by_node": pcp_by_node,
        "present_pages_by_zone": present_pages,
        "managed_pages_by_zone": managed_pages,
    }


def parse_count(field: str) -> Optional[int]:
    cleaned = field.strip().lstrip(">")
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_pagetypeinfo(raw: str, target_order: int) -> Dict[str, Any]:
    zones: Dict[str, Dict[str, Any]] = {}
    for line in raw.splitlines():
        match = PAGETYPE_LINE.match(line)
        if not match:
            continue
        node_id = int(match.group(1))
        zone_name = match.group(2)
        migrate_type = match.group(3)
        counts: List[int] = []
        for field in match.group(4).split():
            count = parse_count(field)
            if count is None:
                break
            counts.append(count)
        if not counts:
            continue
        zone_key = f"node{node_id}_{zone_name}"
        zone = zones.setdefault(
            zone_key,
            {
                "node": node_id,
                "zone": zone_name,
                "types": {},
            },
        )
        type_free = {
            "free_pages": sum(count << order for order, count in enumerate(counts)),
            "suitable_free_pages": sum(
                count << order
                for order, count in enumerate(counts)
                if order >= target_order
            ),
            "unsuitable_free_pages": sum(
                count << order
                for order, count in enumerate(counts)
                if order < target_order
            ),
            "free_order0_blocks": counts[0] if len(counts) > 0 else 0,
            "free_order1_blocks": counts[1] if len(counts) > 1 else 0,
        }
        zone["types"][migrate_type] = type_free

    for zone in zones.values():
        totals = {
            "free_pages": 0,
            "suitable_free_pages": 0,
            "unsuitable_free_pages": 0,
            "free_order0_blocks": 0,
            "free_order1_blocks": 0,
        }
        buckets = {
            "movable": {key: 0 for key in totals},
            "unmovable": {key: 0 for key in totals},
            "other": {key: 0 for key in totals},
        }
        for migrate_type, type_free in zone["types"].items():
            for key in totals:
                totals[key] += type_free[key]
            migrate_type_lower = migrate_type.lower()
            if migrate_type_lower == "movable":
                bucket = "movable"
            elif migrate_type_lower == "unmovable":
                bucket = "unmovable"
            else:
                bucket = "other"
            for key in totals:
                buckets[bucket][key] += type_free[key]
        unsuitable_pages = totals["unsuitable_free_pages"]
        free_pages = totals["free_pages"]
        zone["totals"] = totals
        zone["buckets"] = buckets
        zone["fragmentation_score"] = (
            int(unsuitable_pages * 100 / free_pages) if free_pages else 0
        )

    return {"target_order": target_order, "zones": zones}


DEFAULT_VMSTAT_SUBSTRINGS: Tuple[str, ...] = (
    "order0",
)
DEFAULT_VMSTAT_PREFIXES: Tuple[str, ...] = (
    "order0",
    "alloc_stall",
    "alloc_fail",
    "compact_stall",
    "compact_success",
    "pgscan_",
    "pgsteal_",
    "pgoutrun_",
    "pgwake_",
    "kswapd_order2_",
)


def _parse_vmstat_patterns(raw_values: Optional[Iterable[str]]) -> List[str]:
    """Split --vmstat-key-patterns / VMSTAT_KEY_PATTERNS into a flat pattern list.

    Each raw value may contain comma-separated patterns (e.g. "pgdemote_*,pgmigrate_").
    """
    patterns: List[str] = []
    for raw in raw_values or []:
        for part in raw.split(","):
            part = part.strip()
            if part:
                patterns.append(part)
    return patterns


def select_vmstat_keys(
    vmstat: Dict[str, int],
    patterns: Optional[Iterable[str]] = None,
    mode: str = "append",
) -> List[str]:
    """Select vmstat keys to export into CSV, driven by patterns + mode.

    Pattern syntax:
      * trailing "*" (e.g. "pgdemote_*") = prefix match (startswith)
      * no "*" (e.g. "order0" or "order0_alloc_success") = substring match

    mode:
      * "append" (default): user patterns are added on top of the built-in
        DEFAULT_VMSTAT_PREFIXES / DEFAULT_VMSTAT_SUBSTRINGS whitelist
      * "replace": only user patterns are used, the built-in whitelist is ignored

    Returns sorted unique keys. With patterns=[] + mode="append" this behaves
    exactly like the legacy flatten_vmstat_keys().
    """
    user_patterns = _parse_vmstat_patterns(patterns)
    if mode == "replace":
        substrings: List[str] = []
        prefixes: List[str] = []
    else:
        substrings = list(DEFAULT_VMSTAT_SUBSTRINGS)
        prefixes = list(DEFAULT_VMSTAT_PREFIXES)
    for pattern in user_patterns:
        if pattern.endswith("*"):
            prefixes.append(pattern[:-1])
        else:
            substrings.append(pattern)
    selected: List[str] = []
    for key in vmstat:
        if any(substring in key for substring in substrings):
            selected.append(key)
            continue
        if key.startswith(tuple(prefixes)):
            selected.append(key)
    return sorted(set(selected))


def flatten_vmstat_keys(vmstat: Dict[str, int]) -> List[str]:
    """Backward-compatible wrapper around select_vmstat_keys with default rules."""
    return select_vmstat_keys(vmstat, [], "append")


def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, sort_keys=True) + "\n")


def write_selected_csv(
    records: List[Dict[str, Any]],
    output_path: Path,
    patterns: Optional[Iterable[str]] = None,
    mode: str = "append",
) -> None:
    keys = sorted(
        {
            key
            for record in records
            for key in record.get("vmstat", {})
            if key in select_vmstat_keys(record.get("vmstat", {}), patterns, mode)
        }
    )
    with output_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(["host_ts", "guest_ts", *keys])
        for record in records:
            vmstat = record.get("vmstat", {})
            writer.writerow(
                [
                    record.get("host_ts", ""),
                    record.get("guest_ts", ""),
                    *(vmstat.get(key, "") for key in keys),
                ]
            )


def write_fragmentation_csv(records: List[Dict[str, Any]], output_path: Path) -> None:
    rows: List[Dict[str, Any]] = []
    for record in records:
        fragmentation = record.get("fragmentation", {})
        pcp = record.get("pcp", {})
        for zone_key, zone in fragmentation.get("zones", {}).items():
            totals = zone.get("totals", {})
            buckets = zone.get("buckets", {})
            vmstat = record.get("vmstat", {})
            row: Dict[str, Any] = {
                "host_ts": record.get("host_ts", ""),
                "guest_ts": record.get("guest_ts", ""),
                "zone_key": zone_key,
                "node": zone.get("node", ""),
                "zone": zone.get("zone", ""),
                "free_pages": totals.get("free_pages", 0),
                "suitable_free_pages": totals.get("suitable_free_pages", 0),
                "unsuitable_free_pages": totals.get("unsuitable_free_pages", 0),
                "fragmentation_score": zone.get("fragmentation_score", 0),
                "pcp_order0_zoneinfo": pcp.get("pcp_order0_zoneinfo_by_zone", {}).get(zone_key, 0),
                "nr_pcp_order0_total": vmstat.get("nr_pcp_order0_total", 0),
                "nr_pcp_order0_movable": vmstat.get("nr_pcp_order0_movable", 0),
                "nr_pcp_order0_unmovable": vmstat.get("nr_pcp_order0_unmovable", 0),
                "nr_pcp_order0_other": vmstat.get("nr_pcp_order0_other", 0),
            }
            for bucket_name in ("movable", "unmovable", "other"):
                bucket = buckets.get(bucket_name, {})
                for key in (
                    "unsuitable_free_pages",
                    "free_order0_blocks",
                    "free_order1_blocks",
                ):
                    row[f"{key}_{bucket_name}"] = bucket.get(key, 0)
            rows.append(row)

    fieldnames = sorted({key for row in rows for key in row})
    preferred = [
        "host_ts",
        "guest_ts",
        "zone_key",
        "node",
        "zone",
        "free_pages",
        "suitable_free_pages",
        "unsuitable_free_pages",
        "fragmentation_score",
        "pcp_order0_zoneinfo",
    ]
    fieldnames = [key for key in preferred if key in fieldnames] + [
        key for key in fieldnames if key not in preferred
    ]
    with output_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_delta(records: List[Dict[str, Any]], output_path: Path) -> None:
    if not records:
        output_path.write_text("{}\n", encoding="utf-8")
        return
    first = records[0].get("vmstat", {})
    last = records[-1].get("vmstat", {})
    all_keys = sorted(set(first) | set(last))
    delta: Dict[str, Dict[str, int]] = {}
    for key in all_keys:
        first_value = int(first.get(key, 0))
        last_value = int(last.get(key, 0))
        delta[key] = {
            "start": first_value,
            "end": last_value,
            "delta": last_value - first_value,
        }
    output_path.write_text(json.dumps(delta, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    source_keys = [
        key
        for key in all_keys
        if ("order0" in key or key.startswith("order0"))
        and not key.startswith("nr_pcp_order0_")
        and not key.startswith("order0_pcp_")
    ]
    with output_path.with_name("order0_source_delta.tsv").open(
        "w", encoding="utf-8"
    ) as output:
        output.write("metric\tstart\tend\tdelta\n")
        for key in source_keys:
            values = delta[key]
            output.write(
                f"{key}\t{values['start']}\t{values['end']}\t{values['delta']}\n"
            )

    pcp_keys = [
        key
        for key in all_keys
        if key.startswith("nr_pcp_order0_") or key.startswith("order0_pcp_")
    ]
    with output_path.with_name("pcp_order0_delta.tsv").open(
        "w", encoding="utf-8"
    ) as output:
        output.write("metric\tstart\tend\tdelta\n")
        for key in pcp_keys:
            values = delta[key]
            output.write(
                f"{key}\t{values['start']}\t{values['end']}\t{values['delta']}\n"
            )

    with output_path.with_name("stall_compaction_delta.tsv").open(
        "w", encoding="utf-8"
    ) as output:
        output.write("metric\tstart\tend\tdelta\n")
        for key in all_keys:
            if not key.startswith(("alloc_stall", "alloc_fail", "compact_stall", "compact_success")):
                continue
            values = delta[key]
            output.write(
                f"{key}\t{values['start']}\t{values['end']}\t{values['delta']}\n"
            )


def sample_once(adb: str, serial: str, target_order: int) -> Dict[str, Any]:
    command = (
        "printf '__VMSTAT__\\n'; cat /proc/vmstat; "
        "printf '__PAGETYPEINFO__\\n'; cat /proc/pagetypeinfo 2>/dev/null || true; "
        "printf '__ZONEINFO__\\n'; cat /proc/zoneinfo; "
        "printf '__CMDLINE__\\n'; cat /proc/cmdline; "
        "printf '__SYSCTLS__\\n'; "
        "for path in "
        "/proc/sys/vm/compaction_proactiveness "
        "/proc/sys/vm/compaction_order "
        "/proc/sys/vm/compact_order2_alloc_wake "
        "/proc/sys/vm/kfragd_enabled "
        "/proc/sys/vm/kfragd_frag_high "
        "/proc/sys/vm/kfragd_frag_low "
        "/proc/sys/vm/kfragd_interval_ms "
        "/proc/sys/vm/kfragd_reclaim_batch "
        "/proc/sys/vm/kswapd_order2_threshold "
        "/proc/sys/vm/kswapd_order2_wakeup_threshold "
        "/sys/kernel/mm/transparent_hugepage/defrag "
        "/sys/kernel/mm/transparent_hugepage/enabled "
        "/sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled "
        "/sys/block/zram0/disksize "
        "/sys/block/zram0/comp_algorithm; do "
        "[ -e \"$path\" ] && printf '%s=' \"$path\" && cat \"$path\"; "
        "done"
    )
    return_code, stdout, stderr = run_adb(adb, serial, command, timeout_s=30)
    sections: Dict[str, List[str]] = {}
    current_section = "meta"
    sections[current_section] = []
    for line in stdout.splitlines():
        if line.startswith("__") and line.endswith("__"):
            current_section = line.strip("_").lower()
            sections.setdefault(current_section, [])
            continue
        sections.setdefault(current_section, []).append(line)

    vmstat_raw = "\n".join(sections.get("vmstat", []))
    pagetypeinfo_raw = "\n".join(sections.get("pagetypeinfo", []))
    zoneinfo_raw = "\n".join(sections.get("zoneinfo", []))
    settings = {}
    for line in sections.get("sysctls", []):
        if "=" in line:
            path, value = line.split("=", 1)
            settings[path] = value.strip()

    return {
        "host_ts": time.time(),
        "guest_ts": int(time.time()),
        "adb_return_code": return_code,
        "adb_stderr": stderr[-1000:],
        "vmstat": parse_vmstat(vmstat_raw),
        "fragmentation": parse_pagetypeinfo(pagetypeinfo_raw, target_order),
        "pcp": parse_zoneinfo_pcp(zoneinfo_raw),
        "cmdline": " ".join(sections.get("cmdline", [])).strip(),
        "settings": settings,
    }


def collect(args: argparse.Namespace) -> int:
    output_dir = Path(args.out_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    samples_path = output_dir / "samples.jsonl"
    manifest_path = output_dir / "sampler_manifest.json"
    if args.vmstat_key_patterns is not None:
        patterns_source = "cli"
        patterns_raw: Optional[List[str]] = args.vmstat_key_patterns
    else:
        env_raw = os.environ.get("VMSTAT_KEY_PATTERNS")
        if env_raw:
            patterns_source = "env"
            patterns_raw = [env_raw]
        else:
            patterns_source = "default"
            patterns_raw = None
    patterns = _parse_vmstat_patterns(patterns_raw)
    mode = args.vmstat_keys_mode
    print(
        f"[order0_fragment_sampler] vmstat whitelist source={patterns_source} "
        f"mode={mode} patterns={patterns}",
        file=sys.stderr,
    )
    manifest = {
        "serial": args.serial,
        "adb": str(Path(args.adb).resolve()),
        "interval_s": args.interval_s,
        "target_order": args.target_order,
        "started_at": time.time(),
        "expected": {
            "uffd_mfill_order2": args.expected_uffd_mfill_order2,
            "mthp_cow_order2": args.expected_mthp_cow_order2,
            "kfragd_enabled": args.expected_kfragd_enabled,
            "compact_order2_alloc_wake": args.expected_compact_order2_alloc_wake,
            "kswapd_order2_threshold": args.expected_kswapd_order2_threshold,
            "kswapd_order2_wakeup_threshold": args.expected_kswapd_order2_wakeup_threshold,
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    state = SamplerState()
    signal.signal(signal.SIGINT, state.request_stop)
    signal.signal(signal.SIGTERM, state.request_stop)
    signal.signal(signal.SIGHUP, state.request_stop)

    records: List[Dict[str, Any]] = []
    next_sample = time.monotonic()
    while not state.stop_requested:
        now = time.monotonic()
        if now < next_sample:
            time.sleep(min(next_sample - now, 1.0))
            continue
        record = sample_once(args.adb, args.serial, args.target_order)
        records.append(record)
        append_jsonl(samples_path, record)
        if len(records) == 1:
            matched_keys = select_vmstat_keys(
                record.get("vmstat", {}), patterns, mode
            )
            print(
                f"[order0_fragment_sampler] first sample matched {len(matched_keys)} "
                f"vmstat keys for CSV export",
                file=sys.stderr,
            )
        next_sample += max(1, args.interval_s)

    write_selected_csv(records, output_dir / "order0_vmstat_samples.csv", patterns, mode)
    write_fragmentation_csv(records, output_dir / "fragmentation_samples.csv")
    write_delta(records, output_dir / "vmstat_delta.json")
    manifest["stopped_at"] = time.time()
    manifest["sample_count"] = len(records)
    manifest["adb_error_samples"] = sum(
        1 for record in records if record.get("adb_return_code") != 0
    )
    if records:
        manifest["first_settings"] = records[0].get("settings", {})
        manifest["last_settings"] = records[-1].get("settings", {})
        manifest["first_cmdline"] = records[0].get("cmdline", "")
        manifest["last_cmdline"] = records[-1].get("cmdline", "")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def probe_deploy(args: argparse.Namespace) -> int:
    """Push device-side 20ms probe (C binary + sh fallback), launcher and config."""
    probe_script = Path(args.probe_script).resolve()
    if not probe_script.is_file():
        print(f"missing probe script: {probe_script}", file=sys.stderr)
        return 1
    launcher_script = Path(args.probe_script).parent / "frag20ms_start_probe.sh"
    if not launcher_script.is_file():
        print(f"missing launcher script: {launcher_script}", file=sys.stderr)
        return 1
    probe_binary = Path(args.probe_binary).resolve()
    if not probe_binary.is_file():
        print(f"missing probe binary: {probe_binary}", file=sys.stderr)
        return 1
    remote_dir = args.probe_remote_dir
    rc, out, err = run_adb(args.adb, args.serial, f"mkdir -p {remote_dir}", timeout_s=15)
    if rc != 0:
        print(f"mkdir failed rc={rc} err={err}", file=sys.stderr)
        return 1
    config_text = "\n".join([
        f"INTERVAL_MS={args.probe_interval_ms}",
        f"HIGH={args.probe_high}",
        f"LOW={args.probe_low}",
        f"PROGRESS_S={args.probe_progress_s}",
        f"TRACE_DUMP_S={args.probe_trace_dump_s}",
        "",
    ])
    local_config = Path("/tmp/frag20ms_config.env")
    local_config.write_text(config_text, encoding="utf-8")
    for local, remote in (
        (probe_binary, f"{remote_dir}/frag20ms_probe"),
        (probe_script, f"{remote_dir}/frag20ms_probe.sh"),
        (launcher_script, f"{remote_dir}/frag20ms_start_probe.sh"),
        (local_config, f"{remote_dir}/config.env"),
    ):
        completed = subprocess.run(
            [args.adb, "-s", args.serial, "push", str(local), remote],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            print(f"adb push failed {local} -> {remote}: {completed.stderr}", file=sys.stderr)
            return 1
    rc, out, err = run_adb(
        args.adb, args.serial,
        f"chmod 755 {remote_dir}/frag20ms_probe {remote_dir}/frag20ms_probe.sh {remote_dir}/frag20ms_start_probe.sh",
        timeout_s=15,
    )
    if rc != 0:
        print(f"chmod failed rc={rc} err={err}", file=sys.stderr)
        return 1
    print(f"deployed probe binary={probe_binary.name} script={probe_script.name} config interval_ms={args.probe_interval_ms}")
    return 0


def probe_start(args: argparse.Namespace) -> int:
    """Launch the device-side probe in background via setsid launcher."""
    remote_dir = args.probe_remote_dir
    rc, out, err = run_adb(
        args.adb, args.serial,
        f"sh {remote_dir}/frag20ms_start_probe.sh",
        timeout_s=20,
    )
    if rc != 0:
        print(f"probe start rc={rc} out={out!r} err={err}", file=sys.stderr)
        return 1
    time.sleep(2)
    rc, out, err = run_adb(args.adb, args.serial, f"cat {remote_dir}/progress.txt", timeout_s=15)
    if rc != 0 or not out.strip():
        print(f"probe did not come up rc={rc} out={out!r} err={err}", file=sys.stderr)
        return 1
    print(f"probe started on device: {out.strip()}")
    return 0


def probe_watch(args: argparse.Namespace) -> int:
    """Poll device-side progress file and print it (host log sink)."""
    state = SamplerState()
    signal.signal(signal.SIGINT, state.request_stop)
    signal.signal(signal.SIGTERM, state.request_stop)
    signal.signal(signal.SIGHUP, state.request_stop)
    started = time.monotonic()
    while not state.stop_requested:
        rc, out, err = run_adb(
            args.adb, args.serial,
            f"cat {args.probe_remote_dir}/progress.txt 2>/dev/null || true",
            timeout_s=15,
        )
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        body = out.strip() or "no-progress"
        print(f"{stamp} probe: {body}", flush=True)
        if args.probe_watch_max_s > 0 and time.monotonic() - started >= args.probe_watch_max_s:
            break
        deadline = time.monotonic() + max(1, args.probe_watch_interval_s)
        while not state.stop_requested and time.monotonic() < deadline:
            time.sleep(0.5)
    return 0


def probe_stop(args: argparse.Namespace) -> int:
    """SIGTERM the device-side probe and wait for its cleanup."""
    remote_dir = args.probe_remote_dir
    rc, out, err = run_adb(
        args.adb, args.serial,
        f"cat {remote_dir}/probe.pid 2>/dev/null || true",
        timeout_s=15,
    )
    pid = out.strip()
    if pid and pid.isdigit():
        run_adb(args.adb, args.serial, f"kill -TERM {pid}", timeout_s=15)
    waited = 0
    while waited < 20:
        time.sleep(1)
        waited += 1
        rc, out, err = run_adb(
            args.adb, args.serial,
            f"test -f {remote_dir}/probe.pid && echo alive || echo dead",
            timeout_s=15,
        )
        if "dead" in out:
            break
    rc, out, err = run_adb(args.adb, args.serial, f"cat {remote_dir}/progress.txt 2>/dev/null || true", timeout_s=15)
    if out.strip():
        print(f"probe final: {out.strip()}")
    return 0


def probe_pull(args: argparse.Namespace) -> int:
    """Pull probe artifacts back to host."""
    output_dir = Path(args.out_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    remote_dir = args.probe_remote_dir
    rc = 0
    for name in ("samples.tsv", "trace_dump.txt", "progress.txt", "probe.log"):
        completed = subprocess.run(
            [args.adb, "-s", args.serial, "pull", f"{remote_dir}/{name}", str(output_dir / name)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            rc = 1
            print(f"pull failed {name}: {completed.stderr}", file=sys.stderr)
    return rc


def _unique_ts_intervals(samples_path: Path) -> tuple:
    ts_values: List[float] = []
    with samples_path.open(encoding="utf-8") as handle:
        for line in handle:
            fields = line.split()
            if len(fields) < 2:
                continue
            try:
                value = float(fields[0])
            except ValueError:
                continue
            ts_values.append(value)
    uniq = sorted(set(ts_values))
    if len(uniq) < 2:
        return len(ts_values), len(uniq), [], 0.0, 0.0
    diffs = sorted(uniq[i + 1] - uniq[i] for i in range(len(uniq) - 1))
    n = len(diffs)
    median = diffs[n // 2] if n % 2 else (diffs[n // 2 - 1] + diffs[n // 2]) / 2.0
    p95 = diffs[min(n - 1, int(n * 0.95))]
    return len(ts_values), len(uniq), diffs, median, p95


def probe_verify(args: argparse.Namespace) -> int:
    """Local validation of pulled samples.tsv and trace_dump.txt."""
    output_dir = Path(args.out_dir).resolve()
    samples_path = output_dir / "samples.tsv"
    trace_path = output_dir / "trace_dump.txt"
    verify_lines: List[str] = []
    rc = 0
    if not samples_path.is_file():
        verify_lines.append("FAIL samples.tsv missing")
        rc = 1
    else:
        total, uniq, diffs, median, p95 = _unique_ts_intervals(samples_path)
        verify_lines.append(f"samples_lines={total} unique_ts={uniq}")
        if uniq < 2:
            verify_lines.append("FAIL too few unique timestamps")
            rc = 1
        else:
            verify_lines.append(
                f"ts_interval_median_s={median:.4f} p95_s={p95:.4f} "
                f"min_s={diffs[0]:.4f} max_s={diffs[-1]:.4f}"
            )
            if p95 > 0.12:
                verify_lines.append(f"WARN ts p95 {p95:.4f}s exceeds nominal 0.02s window")
        with samples_path.open(encoding="utf-8") as handle:
            bad_cols = sum(1 for line in handle if len(line.split()) != 11)
        if bad_cols:
            verify_lines.append(f"WARN lines with !=11 cols: {bad_cols}")
    if not trace_path.is_file() or trace_path.stat().st_size == 0:
        verify_lines.append("WARN trace_dump.txt empty/missing")
    else:
        text = trace_path.read_text(encoding="utf-8", errors="replace")
        events = {
            "kfragd_enter": text.count("enter compact loop"),
            "kfragd_exit": text.count("exit compact loop"),
            "kcompactd_prev": text.count("prev_score"),
            "kcompactd_after": text.count("after_score"),
        }
        verify_lines.append(f"trace_events={events}")
        if sum(events.values()) == 0:
            verify_lines.append("FAIL no trace_printk events captured")
            rc = 1
    verify_text = "\n".join(verify_lines)
    print(verify_text)
    (output_dir / "verify.txt").write_text(verify_text + "\n", encoding="utf-8")
    return rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--interval-s", type=int, default=10)
    parser.add_argument("--target-order", type=int, default=2)
    parser.add_argument("--expected-uffd-mfill-order2", type=int, default=1)
    parser.add_argument("--expected-mthp-cow-order2", type=int, default=1)
    parser.add_argument("--expected-kfragd-enabled", type=int, default=0)
    parser.add_argument("--expected-compact-order2-alloc-wake", type=int, default=0)
    parser.add_argument("--expected-kswapd-order2-threshold", type=int, default=0)
    parser.add_argument("--expected-kswapd-order2-wakeup-threshold", type=int, default=0)
    parser.add_argument(
        "--vmstat-key-patterns",
        action="append",
        default=None,
        help=(
            "追加 vmstat 导出白名单规则（order0_vmstat_samples.csv 的列）；可多次指定，"
            "每次也可用逗号分隔多个规则。规则语法：以 * 结尾 = 前缀通配（如 pgdemote_* "
            "匹配所有 pgdemote_ 开头的计数器）；无 * = 子串匹配（如 order0 或 "
            "order0_alloc_success 匹配所有包含该子串的计数器）。未指定时读环境变量 "
            "VMSTAT_KEY_PATTERNS（逗号分隔）；两者都未给时回落到内置默认白名单"
            "（order0 子串 + alloc_stall/alloc_fail/compact_stall/compact_success/"
            "pgscan_/pgsteal_/pgoutrun_/pgwake_/kswapd_order2_ 前缀），行为与旧版一致。"
            "与 --vmstat-keys-mode 配合决定是追加还是替换默认白名单。"
        ),
    )
    parser.add_argument(
        "--vmstat-keys-mode",
        choices=("append", "replace"),
        default="append",
        help=(
            "vmstat 白名单模式：append = 内置默认白名单 + 用户 --vmstat-key-patterns "
            "（默认）；replace = 只用用户 patterns，忽略内置默认白名单（此时若未给 "
            "--vmstat-key-patterns，CSV 将不导出任何 vmstat 计数）。"
        ),
    )
    parser.add_argument(
        "--device-probe",
        choices=("deploy", "start", "watch", "stop", "pull", "verify"),
        help="device-side 20ms probe orchestration subcommand",
    )
    parser.add_argument("--probe-script", default=str(Path(__file__).parent / "frag20ms_probe.sh"))
    parser.add_argument("--probe-binary", default=str(Path(__file__).parent / "frag20ms_probe"))
    parser.add_argument("--probe-remote-dir", default="/data/local/tmp/frag20ms")
    parser.add_argument("--probe-interval-ms", type=int, default=20)
    parser.add_argument("--probe-high", type=int, default=70)
    parser.add_argument("--probe-low", type=int, default=30)
    parser.add_argument("--probe-progress-s", type=int, default=1)
    parser.add_argument("--probe-trace-dump-s", type=int, default=30)
    parser.add_argument("--probe-watch-interval-s", type=int, default=10)
    parser.add_argument("--probe-watch-max-s", type=int, default=0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.device_probe is not None:
        handlers = {
            "deploy": probe_deploy,
            "start": probe_start,
            "watch": probe_watch,
            "stop": probe_stop,
            "pull": probe_pull,
            "verify": probe_verify,
        }
        return handlers[args.device_probe](args)
    return collect(args)


if __name__ == "__main__":
    sys.exit(main())
