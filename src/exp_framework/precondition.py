#!/usr/bin/env python3
"""Standalone memory fragmentation preconditioner.

Run BEFORE memstress/experiment scripts. Ensures all faults allocate order-0
pages, making munmap of 4KB chunks release complete pages without
partial-split issues. THP mode is set by the external launch script, not here.

Usage:
  python3 precondition.py --serial 18281FDF6007HB --alloc-mb 5000
  python3 precondition.py --serial 18281FDF6007HB --alloc-mb 5000 --threshold 2000

After this script prints READY, the fragmem process stays alive holding memory.
Then run your experiment script (which sets sysctl and other configs).
Kill fragmem when done: 由框架权限层统一处理（root/su 自动）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from exp_framework.utils.adb_utils import adb_shell
from exp_framework.fragmem_host import run_fragmem_precondition, stop_fragmem


def main():
    p = argparse.ArgumentParser(description="Standalone memory preconditioner")
    p.add_argument("--serial", required=True, help="Device serial")
    p.add_argument("--alloc-mb", type=int, default=5000,
                   help="Total MB to mmap (default: 5000)")
    p.add_argument("--threshold", type=int, default=2000,
                   help="Target sum(order>=2) in buddyinfo, folded to order-2 equiv (default: 2000)")
    p.add_argument("--chunk-kb", type=int, default=4,
                   help="Chunk size in KB (default: 4)")
    p.add_argument("--stride", type=int, default=2,
                   help="Keep 1 every N chunks (default: 2, keeps 50%%)")
    p.add_argument("--use-su", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--max-map-count", type=int, default=1310720,
                   help="Set vm.max_map_count before fragmem (default: 1310720)")
    p.add_argument("--out-json", default=None,
                   help="Write result to JSON file (optional)")
    args = p.parse_args()

    serial = args.serial

    # Step 0: Reboot device for clean state
    import subprocess
    print(f"[precondition] Rebooting device {serial}...")
    subprocess.run(["adb", "-s", serial, "reboot"], capture_output=True, timeout=15)
    time.sleep(15)
    subprocess.run(["adb", "-s", serial, "wait-for-device"], capture_output=True, timeout=120)
    for _i in range(60):
        cp = subprocess.run(
            ["adb", "-s", serial, "shell", "getprop", "sys.boot_completed"],
            capture_output=True, text=True, timeout=10)
        if cp.stdout.strip() == "1":
            break
        time.sleep(2)
    time.sleep(10)
    # Wait for su (Magisk) to be ready
    print(f"[precondition] Waiting for su to be ready...")
    from utils import adb_utils
    for _i in range(30):
        out = adb_utils.adb_shell_root(serial, "id", timeout_s=10, check=False)
        if "uid=0" in out:
            break
        time.sleep(2)
    print(f"[precondition] Device rebooted and ready")

    # Step 1: Raise max_map_count to allow massive munmap
    print(f"[precondition] Setting max_map_count={args.max_map_count}")
    adb_shell(serial,
        f"echo {args.max_map_count} > /proc/sys/vm/max_map_count",
        timeout_s=10, check=False)

    # Step 2: Run fragmem with retry until threshold met
    max_retries = 3
    alloc_mb = args.alloc_mb
    result = None

    for attempt in range(1, max_retries + 1):
        print(f"[precondition] Attempt {attempt}/{max_retries}: alloc_mb={alloc_mb}")

        # Kill previous fragmem if any
        import subprocess
        adb_utils.adb_shell_root(serial, "killall fragmem", timeout_s=10, check=False)
        time.sleep(1)

        result = run_fragmem_precondition(
            serial,
            alloc_mb=alloc_mb,
            chunk_kb=args.chunk_kb,
            stride=args.stride,
            threshold=args.threshold,
            timeout_s=180,
        )

        sum_order2 = result.get('sum_order2', 99999)
        if sum_order2 <= args.threshold:
            print(f"[precondition] Threshold met: sum_order2={sum_order2} <= {args.threshold}")
            break
        else:
            print(f"[precondition] NOT met: sum_order2={sum_order2} > {args.threshold}")
            # Increase alloc for next attempt
            alloc_mb += 500
            print(f"[precondition] Increasing alloc_mb to {alloc_mb} for retry...")

    if result.get('sum_order2', 99999) > args.threshold:
        print(f"[precondition] ERROR: Could not reach threshold after {max_retries} attempts!")
        print(f"  Final sum_order2={result.get('sum_order2')} > {args.threshold}")
        return 1

    # Step 4: Report
    print(f"\n[precondition] DONE:")
    print(f"  alloc_mb={result.get('alloc_mb')}")
    print(f"  held_mb={result.get('held_mb')}")
    print(f"  sum_order2_equiv={result.get('sum_order2', '?')}")
    print(f"  threshold={args.threshold}")
    print(f"  pid={result.get('pid')}")
    print(f"\n  fragmem is running in background (pid={result.get('pid')}).")
    print(f"  Now set your sysctl config and run your experiment.")
    print(f"  Kill when done: 由框架权限层统一处理（root/su 自动）")

    if args.out_json:
        out = {
            "serial": serial,
            "alloc_mb": result.get("alloc_mb", 0),
            "held_mb": result.get("held_mb", 0),
            "sum_order2_equiv": result.get("sum_order2", -1),
            "threshold": args.threshold,
            "pid": result.get("pid", -1),
            "max_map_count": args.max_map_count,
            "ts": int(time.time()),
        }
        Path(args.out_json).write_text(json.dumps(out, indent=2) + "\n")
        print(f"  Result written to: {args.out_json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
