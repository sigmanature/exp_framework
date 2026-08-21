#!/usr/bin/env python3
"""Memory fragmentation preconditioning for stable short-run experiments.

This script runs a fixed sequence of app launches to fragment device memory
before the actual memstress experiment begins. The goal is to deplete high-order
buddy pages (order >= 2) so that subsequent short runs start from a consistent,
fragmented memory state — eliminating the "warm-up instability" seen in the
first ~30 cycles of a cold-start 120-cycle run.

Strategy:
  1. Rapid burst launches of apps (same package list as memstress) with minimal
     hold time to maximise memory churn.
  2. After each batch, check /proc/buddyinfo. Stop when sum(order>=2) in the
     Normal zone drops below a threshold.
  3. If the threshold is not reached after max_waves, stop anyway (bounded time).

The operation sequence is deterministic given the same seed + package list.

Usage:
  python3 precondition_memory.py --serial <SERIAL> --threshold 2000 --seed 20260617

  # Or integrated into the main workflow:
  python3 run_memstress_and_collect_logs.py --serial <SERIAL> --precondition ...
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Allow running standalone or as imported module
try:
    from utils.adb_utils import adb_shell
    from utils.buddyinfo_utils import parse_buddyinfo
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from utils.adb_utils import adb_shell
    from utils.buddyinfo_utils import parse_buddyinfo


# === Defaults ===
DEFAULT_THRESHOLD = 2000       # sum(order>=2) target in Normal zone
DEFAULT_MAX_WAVES = 20         # max waves before giving up
DEFAULT_BURST_SIZE = 8         # apps per wave (aggressive)
DEFAULT_HOLD_MS = 50           # hold each app briefly
DEFAULT_WAVE_SLEEP_MS = 500    # sleep between waves
DEFAULT_SEED = 20260617
DEFAULT_ZONE = "Normal"


def get_buddyinfo_sum_high_order(
    serial: str,
    zone: str = DEFAULT_ZONE,
    min_order: int = 2,
) -> Tuple[int, Dict[str, List[int]]]:
    """Read /proc/buddyinfo and return sum of order >= min_order for the target zone.

    Returns (sum_value, all_zones_dict).
    """
    try:
        from utils import adb_utils
        out = adb_utils.adb_shell_root(serial, "cat /proc/buddyinfo",
                                       timeout_s=15, tty=True, check=True)
        zones = parse_buddyinfo(out)
    except Exception as e:
        print(f"  [warn] buddyinfo read failed: {e}", file=sys.stderr)
        return -1, {}

    orders = zones.get(zone, [])
    if not orders:
        # Try zone variants (e.g., Normal_node0)
        for k, v in zones.items():
            if zone.lower() in k.lower():
                orders = v
                break

    if len(orders) <= min_order:
        return -1, zones

    high_sum = sum(orders[min_order:])
    return high_sum, zones


def resolve_activity(serial: str, pkg: str) -> Optional[str]:
    """Resolve launcher activity for a package."""
    out = subprocess.run(
        ["adb", "-s", serial, "shell", f"pm resolve-activity --brief {pkg}"],
        capture_output=True, text=True, timeout=10,
    )
    for line in out.stdout.splitlines():
        line = line.strip()
        if "/" in line and not line.startswith("Error"):
            return line
    # fallback
    out2 = subprocess.run(
        ["adb", "-s", serial, "shell",
         f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {pkg}"],
        capture_output=True, text=True, timeout=10,
    )
    for line in out2.stdout.splitlines():
        line = line.strip()
        if "/" in line:
            return line
    return None


def start_activity_fast(serial: str, component: str):
    """Launch an activity without waiting for full start."""
    subprocess.run(
        ["adb", "-s", serial, "shell", "am", "start", "-n", component],
        capture_output=True, timeout=15,
    )


def go_home(serial: str):
    subprocess.run(
        ["adb", "-s", serial, "shell", "input", "keyevent", "KEYCODE_HOME"],
        capture_output=True, timeout=10,
    )


def validate_packages(serial: str, pkgs: List[str]) -> List[str]:
    """Return subset of packages that are installed."""
    out = subprocess.run(
        ["adb", "-s", serial, "shell", "pm", "list", "packages"],
        capture_output=True, text=True, timeout=30,
    )
    installed = set()
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            installed.add(line.split(":", 1)[1])
    return [p for p in pkgs if p in installed]


def load_packages_from_manifest(manifest_path: str) -> List[str]:
    """Load package list from a run_manifest.json or default_memstress_manifest.json."""
    data = json.loads(Path(manifest_path).read_text())
    cfg = data.get("config", data)
    ms = cfg.get("memstress", {})
    return ms.get("packages", [])


def run_precondition(
    serial: str,
    packages: List[str],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    max_waves: int = DEFAULT_MAX_WAVES,
    burst_size: int = DEFAULT_BURST_SIZE,
    hold_ms: int = DEFAULT_HOLD_MS,
    wave_sleep_ms: int = DEFAULT_WAVE_SLEEP_MS,
    seed: int = DEFAULT_SEED,
    zone: str = DEFAULT_ZONE,
    min_order: int = 2,
    out_dir: Optional[Path] = None,
    quiet: bool = False,
) -> dict:
    """Run the preconditioning sequence.

    Returns a dict with:
      - waves_run: number of waves executed
      - final_sum: final sum(order>=min_order)
      - reached_threshold: bool
      - elapsed_s: total seconds
      - history: list of (wave, sum) tuples
    """
    t0 = time.time()

    # Resolve activities
    valid_pkgs = validate_packages(serial, packages)
    if not valid_pkgs:
        raise RuntimeError("No valid packages installed for preconditioning")

    resolved: Dict[str, str] = {}
    for pkg in valid_pkgs:
        comp = resolve_activity(serial, pkg)
        if comp:
            resolved[pkg] = comp
    components = list(resolved.values())

    if not components:
        raise RuntimeError("No launchable activities found for preconditioning")

    if not quiet:
        print(f"[precondition] {len(components)} apps resolved, "
              f"threshold={threshold}, max_waves={max_waves}, burst={burst_size}")

    # Check initial state
    initial_sum, _ = get_buddyinfo_sum_high_order(serial, zone=zone, min_order=min_order)
    if not quiet:
        print(f"[precondition] initial sum(order>={min_order}) = {initial_sum}")

    if 0 <= initial_sum <= threshold:
        if not quiet:
            print(f"[precondition] already below threshold, skipping")
        return {
            "waves_run": 0,
            "final_sum": initial_sum,
            "reached_threshold": True,
            "elapsed_s": round(time.time() - t0, 2),
            "history": [(0, initial_sum)],
        }

    # Run waves
    rng = random.Random(seed)
    history: List[Tuple[int, int]] = [(0, initial_sum)]

    for wave in range(1, max_waves + 1):
        # Shuffle and pick burst
        order = list(components)
        rng.shuffle(order)
        burst = order[:burst_size]

        # Launch burst
        for comp in burst:
            try:
                start_activity_fast(serial, comp)
                time.sleep(hold_ms / 1000.0)
                go_home(serial)
            except Exception:
                pass

        # Brief pause to let memory settle
        time.sleep(wave_sleep_ms / 1000.0)

        # Check buddyinfo
        current_sum, zones_data = get_buddyinfo_sum_high_order(
            serial, zone=zone, min_order=min_order
        )
        history.append((wave, current_sum))

        if not quiet:
            orders_str = ""
            if zones_data:
                z_orders = zones_data.get(zone, [])
                if not z_orders:
                    for k, v in zones_data.items():
                        if zone.lower() in k.lower():
                            z_orders = v
                            break
                if z_orders and len(z_orders) > min_order:
                    orders_str = f" [o2={z_orders[2]} o3={z_orders[3]} o4={z_orders[4] if len(z_orders) > 4 else 0}]"
            print(f"  wave {wave}/{max_waves}: sum(>={min_order})={current_sum}{orders_str}")

        if 0 <= current_sum <= threshold:
            if not quiet:
                print(f"[precondition] threshold reached at wave {wave}")
            break
    else:
        if not quiet:
            print(f"[precondition] max_waves reached, final_sum={current_sum}")

    elapsed = round(time.time() - t0, 2)
    result = {
        "waves_run": wave,
        "final_sum": current_sum,
        "reached_threshold": (0 <= current_sum <= threshold),
        "elapsed_s": elapsed,
        "history": history,
    }

    # Write log if out_dir specified
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "precondition_log.json"
        log_data = {
            **result,
            "config": {
                "threshold": threshold,
                "max_waves": max_waves,
                "burst_size": burst_size,
                "hold_ms": hold_ms,
                "wave_sleep_ms": wave_sleep_ms,
                "seed": seed,
                "zone": zone,
                "min_order": min_order,
                "num_packages": len(components),
            },
        }
        log_path.write_text(
            json.dumps(log_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    if not quiet:
        print(f"[precondition] done in {elapsed:.1f}s, "
              f"waves={wave}, final_sum={current_sum}, "
              f"reached={'yes' if result['reached_threshold'] else 'no'}")

    return result


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Memory fragmentation preconditioning for stable short-run experiments"
    )
    p.add_argument("--serial", required=True, help="Device serial")
    p.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                   help=f"Target sum(order>=min_order) threshold (default: {DEFAULT_THRESHOLD})")
    p.add_argument("--max-waves", type=int, default=DEFAULT_MAX_WAVES,
                   help=f"Maximum preconditioning waves (default: {DEFAULT_MAX_WAVES})")
    p.add_argument("--burst-size", type=int, default=DEFAULT_BURST_SIZE,
                   help=f"Apps per wave (default: {DEFAULT_BURST_SIZE})")
    p.add_argument("--hold-ms", type=int, default=DEFAULT_HOLD_MS,
                   help=f"Hold time per app in ms (default: {DEFAULT_HOLD_MS})")
    p.add_argument("--wave-sleep-ms", type=int, default=DEFAULT_WAVE_SLEEP_MS,
                   help=f"Sleep between waves in ms (default: {DEFAULT_WAVE_SLEEP_MS})")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED,
                   help=f"Random seed (default: {DEFAULT_SEED})")
    p.add_argument("--min-order", type=int, default=2,
                   help="Minimum buddy order to sum (default: 2)")
    p.add_argument("--zone", default=DEFAULT_ZONE,
                   help=f"Buddyinfo zone name (default: {DEFAULT_ZONE})")
    p.add_argument("--use-su", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--from-manifest", default=None,
                   help="Load package list from manifest JSON")
    p.add_argument("--package", action="append", default=None,
                   help="Target package (repeatable)")
    p.add_argument("--package-file", default=None,
                   help="File with one package per line")
    p.add_argument("--out-dir", default=None,
                   help="Output directory for precondition log")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    # Gather packages
    packages: List[str] = []
    if args.from_manifest:
        packages.extend(load_packages_from_manifest(args.from_manifest))
    if args.package:
        packages.extend(args.package)
    if args.package_file:
        with open(args.package_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    packages.append(line)

    # Deduplicate while preserving order
    packages = list(dict.fromkeys(p for p in packages if p))

    if not packages:
        # Default: use the skill's default manifest
        default_manifest = Path(__file__).resolve().parent.parent / "config" / "default_memstress_manifest.json"
        if default_manifest.exists():
            packages = load_packages_from_manifest(str(default_manifest))
        if not packages:
            print("[error] no packages specified", file=sys.stderr)
            return 1

    out_dir = Path(args.out_dir) if args.out_dir else None

    result = run_precondition(
        args.serial,
        packages,
        threshold=args.threshold,
        max_waves=args.max_waves,
        burst_size=args.burst_size,
        hold_ms=args.hold_ms,
        wave_sleep_ms=args.wave_sleep_ms,
        seed=args.seed,
        zone=args.zone,
        min_order=args.min_order,
        out_dir=out_dir,
        quiet=args.quiet,
    )

    if not result["reached_threshold"]:
        return 2  # threshold not reached but not an error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
