#!/usr/bin/env python3
"""memstress 实验兼容入口（薄壳）。

原 CLI 全部保留（--package/--burst-size/--seed/--from-manifest/...），内部
组装输入 config（backend=memstress）后调用统一前端
experiment/runner.run_with_config()。launch_baseline_4k.sh 等外部脚本
无需改动。

新实验请直接用统一入口：
  python3 -m experiment.runner --serial <s> --from-config config/<name>_config.json
"""
from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from exp_framework.utils.adb_utils import ensure_adb_works
from exp_framework.utils.config_utils import deep_merge

from exp_framework.experiment import runner as experiment_runner
from exp_framework.experiment.config import load_default_sample_config


# === 原 memstress 参数（默认值与模板 config 一致） ===

CONFIG = {
    "max_cycles": 1200,
    "interval_s": 60,
    "counters": list(load_default_sample_config()["cycle_sample"]["counters"]["keys"]),
    "memstress": {
        "burst_size": 1,
        "hold_ms": 200,
        "launch_gap_ms": 350,
        "cycle_sleep_ms": 1000,
        "seed": 12345,
        "clear_logcat": True,
        "mode": "launch_only",
        "synthetic_vma_count_scale": 1.0,
        "synthetic_anon_vma_size_scale": 1.0,
        "synthetic_cow_pages_scale": 1.0,
        "synthetic_filemap_size_scale": 1.0,
        "synthetic_dlopen_lib_count_scale": 1.0,
    },
    "buddyinfo_interval_s": 0,
    "vmstat_interval_s": 60,
}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="memstress (slim THP sampler + "
                                            "app launch pressure, compat entry)")

    p.add_argument("--serial", required=True, help="Target device serial")
    p.add_argument("--stop", action="store_true",
                   help="Stop a running experiment on the device")
    p.add_argument("--out-dir", "--out", dest="out_dir", default=None,
                   help="Output directory")
    p.add_argument("--max-cycles", type=int, default=CONFIG["max_cycles"])
    p.add_argument("--interval-s", type=int, default=CONFIG["interval_s"])
    p.add_argument("--counters", default=",".join(CONFIG["counters"]))
    p.add_argument("--package", action="append", default=None,
                   help="Target package (repeatable)")
    p.add_argument("--package-file", default=None,
                   help="File with one package per line")
    p.add_argument("--burst-size", type=int, default=CONFIG["memstress"]["burst_size"])
    p.add_argument("--hold-ms", type=int, default=CONFIG["memstress"]["hold_ms"])
    p.add_argument("--launch-gap-ms", type=int,
                   default=CONFIG["memstress"]["launch_gap_ms"])
    p.add_argument("--cycle-sleep-ms", type=int,
                   default=CONFIG["memstress"]["cycle_sleep_ms"])
    p.add_argument("--seed", type=int, default=CONFIG["memstress"]["seed"])
    p.add_argument("--mode", choices=["launch_only", "interactive"],
                   default=CONFIG["memstress"]["mode"])
    for name in ("synthetic_vma_count_scale", "synthetic_anon_vma_size_scale",
                 "synthetic_cow_pages_scale", "synthetic_filemap_size_scale",
                 "synthetic_dlopen_lib_count_scale"):
        p.add_argument("--" + name, type=float,
                       default=CONFIG["memstress"][name])
    p.add_argument("--clear-logcat", "--no-clear-logcat", dest="clear_logcat",
                   action=argparse.BooleanOptionalAction,
                   default=CONFIG["memstress"]["clear_logcat"])
    p.add_argument("--tasktime-procs", default="",
                   help="Comma-separated process names (comm) to trace with tasktime")
    p.add_argument("--no-crash-detect", action="store_true",
                   help="Disable crash detection and logcat streaming")
    p.add_argument("--buddyinfo-interval-s", type=int,
                   default=CONFIG["buddyinfo_interval_s"])
    p.add_argument("--vmstat-interval-s", type=int,
                   default=CONFIG["vmstat_interval_s"])
    p.add_argument("--from-config", "--from-manifest", dest="from_config",
                   default=None,
                   help="Load all params from an input config JSON "
                        "(旧用法 --from-manifest 同义)")
    p.add_argument("--post-prepare-cmd", default=None)
    p.add_argument("--post-workload-cmd", default=None)
    p.add_argument("--precondition", action="store_true", default=False)
    p.add_argument("--precondition-threshold", type=int, default=2000)
    p.add_argument("--precondition-alloc-mb", type=int, default=4000)

    args = p.parse_args(argv)

    if args.synthetic_anon_vma_size_scale <= 0:
        raise SystemExit("--synthetic-anon-vma-size-scale must be > 0")
    if args.synthetic_cow_pages_scale < 0:
        raise SystemExit("--synthetic-cow-pages-scale must be >= 0")
    if args.synthetic_filemap_size_scale <= 0:
        raise SystemExit("--synthetic-filemap-size-scale must be > 0")

    # --from-config 时读取 sample_config（config 文件驱动；CLI 仅兜底）
    args.sample_config = {}
    if args.from_config:
        try:
            cfg_data = json.loads(Path(args.from_config).read_text(encoding="utf-8"))
            args.sample_config = cfg_data.get("sample_config", {})
        except Exception:
            args.sample_config = {}
    return args


def build_config(args: argparse.Namespace) -> dict:
    """从原 CLI 组装输入 config（backend=memstress）。"""
    packages: List[str] = []
    if args.package:
        packages.extend(args.package)
    if args.package_file:
        from exp_framework.utils.pkg_utils import read_package_file
        packages.extend(read_package_file(args.package_file))
    packages = list(dict.fromkeys(p for p in packages if p))

    tasktime_procs = [p.strip() for p in args.tasktime_procs.split(",") if p.strip()]

    return {
        "config": {
            "counters": args.counters,
            "interval_s": args.interval_s,
            "backend": {
                "name": "memstress",
                "config": {
                    "packages": packages,
                    "max_cycles": args.max_cycles,
                    "burst_size": args.burst_size,
                    "hold_ms": args.hold_ms,
                    "launch_gap_ms": args.launch_gap_ms,
                    "cycle_sleep_ms": args.cycle_sleep_ms,
                    "seed": args.seed,
                    "mode": args.mode,
                    "synthetic_vma_count_scale": args.synthetic_vma_count_scale,
                    "synthetic_anon_vma_size_scale": args.synthetic_anon_vma_size_scale,
                    "synthetic_cow_pages_scale": args.synthetic_cow_pages_scale,
                    "synthetic_filemap_size_scale": args.synthetic_filemap_size_scale,
                    "synthetic_dlopen_lib_count_scale": args.synthetic_dlopen_lib_count_scale,
                    "clear_logcat": bool(args.clear_logcat),
                },
            },
        },
        "sample_config": deep_merge(
            {
                "vmstat": {
                    "interval_s": args.vmstat_interval_s,
                    "buddyinfo": {
                        "enabled": args.buddyinfo_interval_s > 0,
                        "interval_s": args.buddyinfo_interval_s,
                    },
                },
            },
            deep_merge(
                {"tasktime": {"procs": tasktime_procs}} if tasktime_procs else {},
                args.sample_config or {})),
    }


def build_runner_args(args: argparse.Namespace) -> argparse.Namespace:
    """构造 runner 的 Namespace（通用采样参数映射）。"""
    return argparse.Namespace(
        from_config=args.from_config,
        counters=args.counters,
        interval_s=args.interval_s,
        tasktime_procs=args.tasktime_procs,
        no_crash_detect=args.no_crash_detect,
        clear_logcat=bool(args.clear_logcat),
        post_prepare_cmd=args.post_prepare_cmd,
        post_workload_cmd=args.post_workload_cmd,
        precondition=args.precondition,
        precondition_threshold=args.precondition_threshold,
        precondition_alloc_mb=args.precondition_alloc_mb,
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    ensure_adb_works()
    out_dir = Path(args.out_dir) if args.out_dir else Path(
        f"/tmp/thp_memstress_{time.strftime('%Y%m%d_%H%M%S')}")

    if args.stop:
        experiment_runner.send_stop(args.serial)
        return 0

    stop_event = threading.Event()

    def _handler(sig, frame):
        print("\n[stopping]")
        stop_event.set()
        threading.Thread(target=experiment_runner.device_cleanup,
                         args=(args.serial,), daemon=True).start()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

    runner_args = build_runner_args(args)
    try:
        if args.from_config:
            from exp_framework.experiment.config import load_config
            config = load_config(args.from_config)
            manifest = experiment_runner.run_with_config(
                args.serial, out_dir, config, runner_args, stop_event)
        else:
            config = build_config(args)
            manifest = experiment_runner.run_with_config(
                args.serial, out_dir, config, runner_args, stop_event)
    except Exception:
        experiment_runner.device_cleanup(args.serial)
        raise
    print(f"[{manifest['serial']}] done. out_dir={out_dir} "
          f"samples={manifest.get('samples', 0)} "
          f"errors={manifest.get('sample_errors', 0)}")
    return 130 if stop_event.is_set() else 0


if __name__ == "__main__":
    sys.exit(main())
