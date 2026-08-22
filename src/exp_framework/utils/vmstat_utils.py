"""Periodic /proc/vmstat capture for kswapd + direct reclaim monitoring."""

from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import adb_utils


def read_vmstat(serial: str, *, keys: Optional[Sequence[str]] = None) -> Dict[str, int]:
    """Read /proc/vmstat filtered to `keys`（keys=None 时读全部数值行）。"""
    wanted = list(keys) if keys else None
    try:
        out = adb_utils.adb_shell_root(serial, "cat /proc/vmstat", timeout_s=15, tty=True, check=True)
    except Exception:
        return {}
    result: Dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and (wanted is None or parts[0] in wanted):
            try:
                result[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return result


def vmstat_sample_loop(
    *,
    serial: str,
    out_csv: Path,
    interval_s: int,
    stop_event: Optional[object] = None,
    keys: Optional[Sequence[str]] = None,
) -> Tuple[int, int]:
    keys = list(dict.fromkeys(keys or []))
    fieldnames = ["host_ts"] + keys
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    num = 0
    num_err = 0

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()

        next_t = time.time()

        while True:
            if stop_event is not None and hasattr(stop_event, "is_set") and stop_event.is_set():
                break

            now = time.time()
            if now < next_t:
                sleep_interruptible(stop_event, min(next_t - now, 1.0))
                continue

            values = read_vmstat(serial, keys=keys)
            row = {"host_ts": int(time.time())}
            err = False
            for k in keys:
                v = values.get(k)
                if v is not None:
                    row[k] = v
                else:
                    row[k] = 0
                    err = True
            w.writerow(row)
            f.flush()
            num += 1
            if err:
                num_err += 1

            next_t += max(1, interval_s)

    return num, num_err


def derive_vmstat_csv(raw_csv: Path, out_csv: Path) -> int:
    rows: List[Dict[str, str]] = []
    with raw_csv.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    if len(rows) < 2:
        return 0

    numeric_cols = [c for c in rows[0] if c != "host_ts"
                    and str(rows[0][c]).replace(".", "").replace("-", "").isdigit()]
    fieldnames = ["host_ts", "dt_s"] + [f"d_{k}" for k in numeric_cols]
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i in range(1, len(rows)):
            prev, cur = rows[i - 1], rows[i]
            dt = int(cur["host_ts"]) - int(prev["host_ts"])
            derived = {"host_ts": cur["host_ts"], "dt_s": dt}
            for k in numeric_cols:
                derived[f"d_{k}"] = int(cur.get(k, 0)) - int(prev.get(k, 0))
            w.writerow(derived)

    return len(rows) - 1


# ------------------------------------------------------------------ gate 预检

def verify(config: dict) -> list:
    """vmstat keys 校验（gate 预检）：复用 read_vmstat。

    config 约定：config["sample_config"]["vmstat"]["keys"]，
    config["_ctx"] = {"serial"}。
    """
    from typing import Any, Dict, List
    ctx = config.get("_ctx", {})
    serial = ctx.get("serial")
    keys = (config.get("sample_config") or {}).get("vmstat", {}).get("keys") or []
    if not keys:
        return []
    values = read_vmstat(serial, keys=keys)
    missing = [k for k in keys if k not in values]
    print(f"  {'vmstat_keys':<28s} = {len(keys) - len(missing)}/{len(keys)} 存在 "
          f"[{'OK' if not missing else 'MISMATCH(缺 ' + str(missing) + ')'}]")
    return [{"param": "vmstat_keys",
             "expected": f"{len(keys)} 个键存在",
             "actual": f"缺 {len(missing)} 个",
             "ok": not missing}]
