"""Periodic /proc/buddyinfo capture from Android devices."""

from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import adb_utils


MAX_ORDER = 10


def parse_buddyinfo(text: str) -> Dict[str, List[int]]:
    zones: Dict[str, List[int]] = {}
    for line in text.splitlines():
        tokens = line.strip().split()
        if len(tokens) < 4:
            continue
        # /proc/buddyinfo format:
        #   Node <N>, zone <Name>  <order_0>  <order_1>  ...
        node_str = tokens[1].rstrip(",")
        zone_name = tokens[3]

        orders: List[int] = []
        for token in tokens[4:]:
            try:
                orders.append(int(token))
            except ValueError:
                pass

        if not orders:
            continue

        label = zone_name
        if zone_name in zones:
            label = f"{zone_name}_node{node_str}"
        zones[label] = orders

    return zones


def flatten_buddyinfo_for_csv(zones: Dict[str, List[int]]) -> Dict[str, int]:
    flat: Dict[str, int] = {}
    for zone_label, orders in zones.items():
        for i, count in enumerate(orders):
            flat[f"{zone_label}_order_{i}"] = count
    return flat


def buddyinfo_fieldnames(order_count: int = MAX_ORDER + 1) -> List[str]:
    return ["host_ts"] + [f"Normal_order_{i}" for i in range(order_count)]


def read_buddyinfo_once(serial: str) -> Tuple[int, Dict[str, int], str]:
    host_ts = int(time.time())
    try:
        out = adb_utils.adb_shell_root(serial, "cat /proc/buddyinfo", timeout_s=15, tty=True, check=True)
        zones = parse_buddyinfo(out)
        flat = flatten_buddyinfo_for_csv(zones)
        return host_ts, flat, ""
    except Exception as e:
        return host_ts, {}, str(e)


def buddyinfo_sample_loop(
    *,
    serial: str,
    out_csv: Path,
    interval_s: int,
    stop_event: Optional[object] = None,
) -> Tuple[int, int]:
    first_ts, first_flat, first_err = read_buddyinfo_once(serial)

    if first_err:
        fieldnames = buddyinfo_fieldnames()
    else:
        fieldnames = ["host_ts"] + sorted(first_flat.keys())

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    num = 0
    num_err = 0

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()

        row = {"host_ts": first_ts}
        for k in fieldnames[1:]:
            row[k] = first_flat.get(k, "")
        w.writerow(row)
        f.flush()
        num += 1
        if first_err:
            num_err += 1

        next_t = time.time() + interval_s

        while True:
            if stop_event is not None and hasattr(stop_event, "is_set") and stop_event.is_set():
                break

            now = time.time()
            if now < next_t:
                sleep_interruptible(stop_event, min(next_t - now, 1.0))
                continue

            ts, flat, err = read_buddyinfo_once(serial)
            row = {"host_ts": ts}
            for k in fieldnames[1:]:
                row[k] = flat.get(k, "")
            w.writerow(row)
            f.flush()
            num += 1
            if err:
                num_err += 1

            next_t += max(1, interval_s)

    return num, num_err


# ---------------------------------------------------------------------------
# Combined buddyinfo + THP counter sampling
# ---------------------------------------------------------------------------

def read_buddyinfo_and_thp_once(
    serial: str,
    *,
    counters: List[str],
    stats_dir: str,
) -> Tuple[int, Dict[str, int], Dict[str, Optional[int]], str]:
    """Read /proc/buddyinfo and THP stats counters in one adb call.

    Returns (host_ts, buddy_flat, thp_values, error).
    """
    host_ts = int(time.time())

    parts = ["cat /proc/buddyinfo"]
    for c in counters:
        parts.append(f"echo THP_{c}=$(cat {stats_dir}/{c} 2>/dev/null || echo '')")

    script = "; ".join(parts)

    try:
        out = adb_utils.adb_shell_root(serial, script, timeout_s=20, check=True)
        # Split output: first section is /proc/buddyinfo, rest are THP lines
        sections = out.split("\nTHP_", 1)
        buddy_text = sections[0]
        thp_text = "THP_" + sections[1] if len(sections) > 1 else ""

        zones = parse_buddyinfo(buddy_text)
        flat = flatten_buddyinfo_for_csv(zones)

        thp_values: Dict[str, Optional[int]] = {}
        for line in thp_text.splitlines():
            line = line.strip()
            if "=" in line and line.startswith("THP_"):
                k, v = line.split("=", 1)
                k = k[4:]  # strip "THP_" prefix
                thp_values[k] = int(v.strip()) if v.strip().isdigit() else None

        return host_ts, flat, thp_values, ""
    except Exception as e:
        return host_ts, {}, {}, str(e)


def buddyinfo_with_thp_sample_loop(
    *,
    serial: str,
    out_csv: Path,
    interval_s: int,
    counters: List[str],
    stats_dir: str,
    stop_event: Optional[object] = None,
) -> Tuple[int, int]:
    """Sample buddyinfo + THP counters at the same frequency.

    CSV columns: host_ts, Normal_order_0..Normal_order_10, <counter_1>, <counter_2>, ...
    """
    first_ts, first_flat, first_thp, first_err = read_buddyinfo_and_thp_once(
        serial, counters=counters, stats_dir=stats_dir
    )

    buddy_cols = sorted(first_flat.keys()) if first_flat else [f"Normal_order_{i}" for i in range(MAX_ORDER + 1)]
    thp_cols = [c for c in counters]

    if first_err:
        fieldnames = ["host_ts"] + buddy_cols + thp_cols
    else:
        fieldnames = ["host_ts"] + buddy_cols + thp_cols

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    num = 0
    num_err = 0

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()

        row = {"host_ts": first_ts}
        for k in buddy_cols:
            row[k] = first_flat.get(k, "")
        for c in thp_cols:
            row[c] = first_thp.get(c, "")
        w.writerow(row)
        f.flush()
        num += 1
        if first_err:
            num_err += 1

        next_t = time.time() + interval_s

        while True:
            if stop_event is not None and hasattr(stop_event, "is_set") and stop_event.is_set():
                break

            now = time.time()
            if now < next_t:
                sleep_interruptible(stop_event, min(next_t - now, 1.0))
                continue

            ts, flat, thp, err = read_buddyinfo_and_thp_once(
                serial, counters=counters, stats_dir=stats_dir
            )
            row = {"host_ts": ts}
            for k in buddy_cols:
                row[k] = flat.get(k, "")
            for c in thp_cols:
                row[c] = thp.get(c, "")
            w.writerow(row)
            f.flush()
            num += 1
            if err:
                num_err += 1

            next_t += max(1, interval_s)

    return num, num_err
