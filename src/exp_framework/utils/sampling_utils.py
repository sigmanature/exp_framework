from __future__ import annotations

import csv
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .adb_utils import run
from . import adb_utils
from .signal_utils import sleep_interruptible


DEFAULT_STATS_DIR = "/sys/kernel/mm/transparent_hugepage/hugepages-16kB/stats"

FOLIO_ALLOC_KEYS = [f"order_{i}" for i in range(16)] + ["folio_large_total", "folio_alloc_total"]


@dataclass
class Sample:
    host_ts: int
    device_ts: Optional[int]
    values: Dict[str, Optional[int]]
    error: str = ""


def parse_kv_lines(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def read_counters_once(serial: str, stats_dir: str, counters: Sequence[str]) -> Sample:
    host_ts = int(time.time())

    parts = [
        "ts=$(date +%s)",
        "echo device_ts=$ts",
    ]
    for c in counters:
        parts.append(f"v=$(cat {stats_dir}/{c} 2>/dev/null || echo '')")
        parts.append(f"echo {c}=$v")
    parts.append("cat /sys/kernel/mm/readahead/folio_alloc 2>/dev/null || true")

    script = "; ".join(parts)

    try:
        out = adb_utils.adb_shell_root(serial, script, timeout_s=20, check=True)
        kv = parse_kv_lines(out)
        dev_ts = int(kv.get("device_ts")) if kv.get("device_ts", "").isdigit() else None
        values: Dict[str, Optional[int]] = {}
        for c in counters:
            s = kv.get(c, "")
            values[c] = int(s) if s.isdigit() else None

        # Parse folio_alloc order_N counters
        for key in FOLIO_ALLOC_KEYS[:-2]:
            s = kv.get(key, "")
            values[key] = int(s) if s.isdigit() else None

        # Compute large folio totals
        large_total = 0
        total = 0
        for i in range(16):
            key = f"order_{i}"
            val = values.get(key)
            if val is not None:
                total += val
                if i > 0:
                    large_total += val
        values["folio_large_total"] = large_total
        values["folio_alloc_total"] = total

        return Sample(host_ts=host_ts, device_ts=dev_ts, values=values, error="")
    except Exception as e:
        err_values: Dict[str, Optional[int]] = {str(c): None for c in counters}
        for key in FOLIO_ALLOC_KEYS:
            err_values[key] = None
        return Sample(host_ts=host_ts, device_ts=None, values=err_values, error=str(e))


def sample_loop(
    *,
    serial: str,
    stats_dir: str,
    counters: Sequence[str],
    interval_s: int,
    out_csv: Path,
    retries: int,
    retry_sleep_s: int,
    stop_event: Optional[object] = None,
) -> Tuple[int, int]:
    """Returns (num_samples, num_errors).

    Runs indefinitely until stop_event is set. Sampling interval is fixed.
    stop_event: optional `threading.Event`-like object with `.is_set()`.
    """

    fieldnames = ["host_ts", "device_ts", "error"] + list(counters) + FOLIO_ALLOC_KEYS
    next_t = time.time()

    num = 0
    num_err = 0

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()

        while True:
            if stop_event is not None and getattr(stop_event, "is_set", None) and stop_event.is_set():
                break

            now = time.time()
            if now < next_t:
                sleep_interruptible(stop_event, min(next_t - now, 1.0))
                continue

            s: Optional[Sample] = None
            for _attempt in range(max(1, retries + 1)):
                s = read_counters_once(serial, stats_dir, counters)
                if not s.error:
                    break
                sleep_interruptible(stop_event, max(0, retry_sleep_s))

            assert s is not None
            row = {
                "host_ts": s.host_ts,
                "device_ts": s.device_ts if s.device_ts is not None else "",
                "error": s.error,
            }
            for c in counters + FOLIO_ALLOC_KEYS:
                v = s.values.get(str(c))
                row[str(c)] = v if v is not None else ""
            w.writerow(row)
            f.flush()

            num += 1
            if s.error:
                num_err += 1

            next_t += max(1, interval_s)

    return num, num_err


def run_derive_metrics(*, scripts_dir: Path, out_dir: Path,
                       vmstat_start: Optional[Path] = None,
                       vmstat_end: Optional[Path] = None) -> None:
    derive = scripts_dir / "derive_metrics.py"
    cmd = [sys.executable, str(derive), str(out_dir / "raw_samples.csv"), "--out-dir", str(out_dir)]
    if vmstat_start and vmstat_start.exists():
        cmd.extend(["--vmstat-start", str(vmstat_start)])
    if vmstat_end and vmstat_end.exists():
        cmd.extend(["--vmstat-end", str(vmstat_end)])
    cp = run(cmd, timeout_s=120, check=False)
    (out_dir / "derive_stdout.txt").write_text(cp.stdout or "", encoding="utf-8")
    (out_dir / "derive_stderr.txt").write_text(cp.stderr or "", encoding="utf-8")
    if cp.returncode != 0:
        raise RuntimeError(f"derive_metrics failed rc={cp.returncode}. See derive_stderr.txt")


def write_run_manifest(path: Path, obj: Dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

