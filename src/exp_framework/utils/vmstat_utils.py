"""Periodic /proc/vmstat capture for kswapd + direct reclaim monitoring."""

from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import adb_utils


VMSTAT_KEYS = [
    "allocstall_dma",
    "allocstall_dma32",
    "allocstall_highmem",
    "allocstall_device",
    "allocstall_normal",
    "allocstall_movable",
    "pgscan_direct",
    "pgsteal_direct",
    "pgscan_kswapd",
    "pgsteal_kswapd",
    "pgscan_anon",
    "pgscan_file",
    "pgsteal_anon",
    "pgsteal_file",
    "pgscan_direct_throttle",
    "compact_stall",
    "compact_success",
    "compact_daemon_wake",
    "compact_daemon_migrate_scanned",
    "compact_daemon_free_scanned",
    "compact_daemon_order2_created",
    "alloc_success_order0",
    "alloc_success_order2",
    "alloc_fail_wmark",
    "alloc_fail_fragment",
    "alloc_stall_wmark",
    "alloc_stall_fragment",
    "alloc_fail_wmark_anon",
    "alloc_fail_wmark_file",
    "alloc_fail_fragment_anon",
    "alloc_fail_fragment_file",
    "alloc_stall_wmark_anon",
    "alloc_stall_wmark_file",
    "alloc_stall_fragment_anon",
    "alloc_stall_fragment_file",
    "anon_mthp_vma_unsuitable_order2",
    "cow_mthp_order2",
    "cow_mthp_fallback_order0",
    "cow_mthp_vma_unsuitable_order2",
    "vma_anon_alloc_order0_total",
    "vma_anon_alloc_order2_total",
    "vma_anon_alloc_order0_anon_fault",
    "vma_anon_alloc_order2_anon_fault",
    "vma_anon_alloc_order0_swapin",
    "vma_anon_alloc_order2_swapin",
    "vma_anon_alloc_order0_cow",
    "vma_anon_alloc_order2_cow",
    "vma_anon_alloc_order0_file_cow",
    "vma_anon_alloc_order2_file_cow",
    "vma_anon_alloc_order0_uffd_copy",
    "vma_anon_alloc_order2_uffd_copy",
    "vma_anon_alloc_order0_uffd_zeropage",
    "vma_anon_alloc_order2_uffd_zeropage",
    "vma_anon_alloc_order0_class_dalvik",
    "vma_anon_alloc_order2_class_dalvik",
    "vma_anon_alloc_order0_class_scudo",
    "vma_anon_alloc_order2_class_scudo",
    "vma_anon_alloc_order0_class_bionic",
    "vma_anon_alloc_order0_class_mthp_vma",
    "vma_anon_alloc_order2_class_bionic",
    "vma_anon_alloc_order2_class_mthp_vma",
    "vma_anon_alloc_order0_class_stack",
    "vma_anon_alloc_order2_class_stack",
    "vma_anon_alloc_order0_class_file_private",
    "vma_anon_alloc_order2_class_file_private",
    "vma_anon_alloc_order0_class_other",
    "vma_anon_alloc_order2_class_other",
    "file_alloc_order0_total",
    "file_alloc_order2_total",
    "file_alloc_order0_unknown",
    "file_alloc_order2_unknown",
    "file_alloc_order0_readahead",
    "file_alloc_order2_readahead",
    "file_alloc_order0_getfolio_mmap",
    "file_alloc_order2_getfolio_mmap",
    "file_alloc_order0_getfolio_write",
    "file_alloc_order2_getfolio_write",
    "file_alloc_order0_getfolio_other",
    "file_alloc_order2_getfolio_other",
    "file_alloc_order0_buffered_read",
    "file_alloc_order2_buffered_read",
    "file_alloc_order0_read_cache",
    "file_alloc_order2_read_cache",
    "uffd_mfill_order2_attempt_copy",
    "uffd_mfill_order2_attempt_zeropage",
    "uffd_mfill_order2_success_copy",
    "uffd_mfill_order2_success_zeropage",
    "uffd_mfill_order0_success_copy",
    "uffd_mfill_order0_success_zeropage",
    "order0_binder_buffer_page",
    "order0_tlb_gather_batch_page",
    "order0_tlb_table_batch_page",
    "order0_pte_alloc_page",
    "order0_pmd_alloc_page",
    "order0_vmalloc_page",
    "order0_slub_new_slab_page",
    "order0_zsmalloc_page",
    "order2_zsmalloc_page",
    "dmabuf_alloc_order0",
    "fork_dup_task_struct_event",
    "fork_vmap_stack_pages_expected",
    "fork_nonvmap_stack_order_pages",
    "scudo_vma_unsuitable_order2",
    "dalvik_vma_unsuitable_order2",
    "scudo_pte_occupied_order2",
    "dalvik_pte_occupied_order2",
    "bionic_vma_unsuitable_order2",
    "mthp_vma_unsuitable_order2",
    "stack_vma_unsuitable_order2",
    "other_vma_unsuitable_order2",
    "bionic_pte_occupied_order2",
    "mthp_vma_pte_occupied_order2",
    "stack_pte_occupied_order2",
    "other_pte_occupied_order2",
    "anon_mthp_order2_vma_left_boundary",
    "anon_mthp_order2_vma_right_boundary",
    "anon_mthp_order2_vma_too_small",
    "anon_mthp_order2_not_allowed_dalvik",
    "anon_mthp_order2_not_allowed_scudo",
    "anon_mthp_order2_not_allowed_bionic",
    "anon_mthp_order2_not_allowed_mthp_vma",
    "anon_mthp_order2_not_allowed_stack",
    "anon_mthp_order2_not_allowed_other",
    "anon_fault_order0_class_dalvik",
    "anon_fault_order0_class_scudo",
    "anon_fault_order0_class_bionic",
    "anon_fault_order0_class_mthp_vma",
    "anon_fault_order0_class_stack",
    "anon_fault_order0_class_other",
    "anon_mthp_order2_uffd_armed",
    "anon_mthp_order2_vma_not_allowed",
    "anon_mthp_order2_vma_suitable_filtered",
    "anon_mthp_order2_pte_range_not_empty",
    "anon_mthp_order2_alloc_fail",
    "anon_mthp_order2_memcg_fail",
    "cow_mthp_order2_fallback_old_folio_not_order2",
    "cow_mthp_order2_fallback_vma_not_allowed",
    "cow_mthp_order2_fallback_alloc_fail",
    "cow_mthp_order2_fallback_pte_changed_or_unusable",
    "kcompactd_timeout_wake",
    "kcompactd_wake_request",
    "kcompactd_woke_by_alloc",
    "kcompactd_woke_by_vmscan",
    "kcompactd_order2_low",
    "kcompactd_wake_from_vmscan",
    "kcompactd_wake_from_alloc",
    "pageoutrun",
    "pgoutrun_order2_b0",
    "pgoutrun_order2_b1",
    "pgoutrun_order2_b2_3",
    "pgoutrun_order2_b4_7",
    "pgoutrun_order2_b8_15",
    "pgoutrun_order2_b16_31",
    "pgoutrun_order2_b32_63",
    "pgoutrun_order2_b64_127",
    "pgoutrun_order2_b128_255",
    "pgoutrun_order2_b256_511",
    "pgoutrun_order2_b512_1023",
    "pgoutrun_order2_b1024_2047",
    "pgoutrun_order2_b2048_4095",
    "pgoutrun_order2_b4096_inf",
    "kswapd_inodesteal",
    "pgfault",
    "pgmajfault",
    "workingset_refault_anon",
    "workingset_refault_file",
    "pswpout",
    "zswpout",
    "swpout_zero",
    "thp_fault_alloc",
    "thp_fault_fallback",
    "thp_swpout",
    "thp_swpout_fallback",
]

for order in range(0, 11):
    for reclaimer in ("kswapd", "direct", "khugepaged", "proactive"):
        key = f"pgsteal_order{order}_{reclaimer}"
        if key not in VMSTAT_KEYS:
            VMSTAT_KEYS.append(key)

_ORDER2_BUCKETS = [
    "b0",
    "b1",
    "b2_3",
    "b4_7",
    "b8_15",
    "b16_31",
    "b32_63",
    "b64_127",
    "b128_255",
    "b256_511",
    "b512_1023",
    "b1024_2047",
    "b2048_4095",
    "b4096_inf",
]

for bucket in _ORDER2_BUCKETS:
    for prefix in ("pgoutrun_order2", "pgwake_order2_first"):
        key = f"{prefix}_{bucket}"
        if key not in VMSTAT_KEYS:
            VMSTAT_KEYS.append(key)

for bucket in (
    "b0_99",
    "b100_499",
    "b500_999",
    "b1000_4999",
    "b5000_9999",
    "b10000_49999",
    "b50000_inf",
):
    key = f"pgwake_to_pgoutrun_us_{bucket}"
    if key not in VMSTAT_KEYS:
        VMSTAT_KEYS.append(key)

for bucket in (
    "neg4096_inf",
    "neg2048_4095",
    "neg1024_2047",
    "neg1_1023",
    "zero",
    "pos1_1023",
    "pos1024_2047",
    "pos2048_4095",
    "pos4096_inf",
):
    key = f"pgwake_to_pgoutrun_delta_{bucket}"
    if key not in VMSTAT_KEYS:
        VMSTAT_KEYS.append(key)

for bucket in ("b1", "b2_3", "b4_7", "b8_15", "b16_inf"):
    key = f"kswapd_order2_iters_{bucket}"
    if key not in VMSTAT_KEYS:
        VMSTAT_KEYS.append(key)


def read_vmstat(serial: str, *, keys: Optional[Sequence[str]] = None) -> Dict[str, int]:
    """Read /proc/vmstat filtered to `keys` (default: VMSTAT_KEYS)."""
    wanted = list(keys) if keys else VMSTAT_KEYS
    try:
        out = adb_utils.adb_shell_root(serial, "cat /proc/vmstat", timeout_s=15, tty=True, check=True)
    except Exception:
        return {}
    result: Dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in wanted:
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
    keys = list(dict.fromkeys(list(VMSTAT_KEYS) + list(keys or [])))
    # per-sample CPU freq + thermal (bypass observers; one adb call)
    freq_cols = [f"freq_cpu{i}" for i in range(8)]
    temp_cols = ["temp_BIG", "temp_MID", "temp_LITTLE", "temp_G3D", "temp_battery"]
    fieldnames = ["host_ts"] + keys + freq_cols + temp_cols
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
                time.sleep(min(next_t - now, 1.0))
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
            # bypass sample of cur_freq (8 cores) + 5 thermal zones (one adb call)
            try:
                extra = adb_utils.adb_shell_root(
                    serial,
                    "for i in 0 1 2 3 4 5 6 7; do "
                    "cat /sys/devices/system/cpu/cpu$i/cpufreq/scaling_cur_freq 2>/dev/null; done; "
                    "echo ---; "
                    "for z in thermal_zone0 thermal_zone1 thermal_zone2 thermal_zone3 "
                    "thermal_zone25; do cat /sys/class/thermal/$z/temp 2>/dev/null; done",
                    timeout_s=10, check=False,
                )
                parts = str(extra).split("---")
                if len(parts) >= 2:
                    freqs = [t for t in parts[0].split() if t.replace(".", "").isdigit()][:8]
                    temps = [t for t in parts[1].split() if t.replace(".", "").isdigit()][:5]
                    for i, fv in enumerate(freqs):
                        row[freq_cols[i]] = int(float(fv))
                    for i, tv in enumerate(temps):
                        row[temp_cols[i]] = int(float(tv)) / 1000.0
            except Exception:
                pass
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

    fieldnames = ["host_ts", "dt_s"] + [f"d_{k}" for k in VMSTAT_KEYS]
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i in range(1, len(rows)):
            prev, cur = rows[i - 1], rows[i]
            dt = int(cur["host_ts"]) - int(prev["host_ts"])
            derived = {"host_ts": cur["host_ts"], "dt_s": dt}
            for k in VMSTAT_KEYS:
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
