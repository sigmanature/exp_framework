"""实验前端统一入口：prepare -> sample_start -> backend.run -> sample_end。

用法：
  python3 -m experiment.runner --serial <s> --from-config config/baseline_4k_config.json
  python3 -m experiment.runner --serial <s> --from-config <cfg> --stop

信号（SIGINT/SIGTERM）：置位 stop_event + 启动设备清理线程；
backend.run() 会尽快退出，sample_end 在 finally 中必然执行。
"""
import argparse
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.adb_utils import ensure_adb_works

import experiments  # noqa: F401  (注册后端副作用)
from experiment.experiment import create_experiment
from experiment.config import (load_config, backend_from_config,
                               new_run_manifest, write_run_manifest,
                               resolve_sample_config)
from experiment.sample import sample_start, sample_end


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Experiment runner (frontend)")
    p.add_argument("--serial", required=True, help="Target device serial")
    p.add_argument("--stop", action="store_true",
                   help="Stop a running experiment on the device")
    p.add_argument("--out-dir", "--out", dest="out_dir", default=None,
                   help="Output directory")
    p.add_argument("--from-config", default=None,
                   help="Input config JSON (see config/ for templates)")
    # ---- 全局/采样参数（后端无关；config 文件优先）----
    p.add_argument("--counters", default="")
    p.add_argument("--interval-s", type=int, default=60)
    p.add_argument("--tasktime-procs", default="")
    p.add_argument("--no-network-check", action="store_true")
    p.add_argument("--no-crash-detect", action="store_true")
    p.add_argument("--clear-logcat", action="store_true")
    p.add_argument("--post-prepare-cmd", default=None)
    p.add_argument("--post-workload-cmd", default=None)
    p.add_argument("--precondition", action="store_true", default=False)
    p.add_argument("--precondition-threshold", type=int, default=2000)
    p.add_argument("--precondition-alloc-mb", type=int, default=4000)
    # ---- 后端选择（未用 --from-config 时）----
    p.add_argument("--backend", default="memstress",
                   help="backend name (registered in experiment.REGISTRY)")
    p.add_argument("--backend-config", default=None,
                   help="path to JSON file with the backend-specific config")
    return p.parse_args(argv)


def _load_input_config(args: argparse.Namespace) -> Dict[str, Any]:
    """从 --from-config 或（--backend + --backend-config）组装输入 config。"""
    if args.from_config:
        return load_config(args.from_config)
    backend_cfg = {}
    if args.backend_config:
        backend_cfg = load_config(args.backend_config)
    return {
        "config": {
            "counters": args.counters,
            "interval_s": args.interval_s,
            "backend": {"name": args.backend, "config": backend_cfg},
        },
        "sample_config": {},
    }


def _global_overrides(args: argparse.Namespace,
                      manifest: Dict[str, Any]) -> Dict[str, Any]:
    """CLI 显式参数覆盖 config 文件（config 文件优先，CLI 仅填空）。"""
    cfg = manifest.get("config", {})
    if args.counters:
        cfg["counters"] = args.counters
    if args.interval_s != 60:
        cfg["interval_s"] = args.interval_s
    manifest["config"] = cfg
    return manifest


def run_with_config(serial: str, out_dir: Path, input_cfg: Dict[str, Any],
                    args: argparse.Namespace,
                    stop_event: threading.Event) -> Dict[str, Any]:
    """按输入 config 跑一次实验（框架主流程）。薄壳可绕过 CLI 直接调用。"""
    out_dir.mkdir(parents=True, exist_ok=True)

    # 0) 探测设备权限模式（root / su，自动选择；失败早停）
    from utils import adb_utils
    adb_utils.ensure_privilege(serial)

    # 1) 输入 config -> 后端参数 + 全局参数 + 采样配置
    name, backend_cfg, global_cfg = backend_from_config(input_cfg)
    manifest = new_run_manifest(serial, input_cfg)
    manifest = _global_overrides(args, manifest)
    manifest_path = out_dir / "run_manifest.json"
    write_run_manifest(manifest, manifest_path)

    # 2) 实例化后端（注册为活跃后端，供信号/清理立即停设备端）
    global _ACTIVE_BACKEND
    backend = create_experiment(name, backend_cfg, global_cfg, serial,
                                out_dir, stop_event)
    _ACTIVE_BACKEND = backend

    # 3) prepare（设备准备 + 后端预检；失败早停，采样不启动）
    private_fields = backend.prepare()
    if private_fields:
        manifest.update(private_fields)
        write_run_manifest(manifest, manifest_path)

    # 4) sample_start
    sample_cfg = resolve_sample_config(manifest.get("sample_config", {}))
    resolved_pkgs = list((private_fields or {}).get("packages_resolved", {}).keys())
    sess = sample_start(serial, out_dir, sample_cfg, manifest["config"],
                        args, stop_event, resolved_pkgs=resolved_pkgs)

    # 5) backend.run + 6) sample_end（finally 保证收尾）
    backend_result: Dict[str, Any] = {}
    try:
        backend_result = backend.run() or {}
        if backend_result:
            manifest.update(backend_result)
    finally:
        try:
            sample_end(serial, out_dir, sample_cfg, manifest["config"],
                       args, sess)
        finally:
            try:
                backend.cleanup()
            except Exception as e:
                print(f"[{serial}] backend cleanup failed: {e}", file=sys.stderr)

    _ACTIVE_BACKEND = None

    # 7) manifest 收尾
    manifest["status"] = "stopped" if stop_event.is_set() else "finished"
    manifest["end_host_ts"] = int(time.time())
    manifest["samples"] = sess.sampling_result["samples"]
    manifest["sample_errors"] = sess.sampling_result["errors"]
    write_run_manifest(manifest, manifest_path)
    return manifest


def run_one_device(serial: str, out_dir: Path, args: argparse.Namespace,
                   stop_event: threading.Event) -> Dict[str, Any]:
    """CLI 入口版：从 --from-config / --backend 组装输入 config。"""
    input_cfg = _load_input_config(args)
    return run_with_config(serial, out_dir, input_cfg, args, stop_event)


# ---------------- 信号 / 清理 ----------------

_ACTIVE_BACKEND = None  # 当前运行的后端（供信号/清理时调用 stop_device）


def device_cleanup(serial: str):
    """框架统一设备清理：后端设备端停止 + trace probe / tasktime / tracing。"""
    global _ACTIVE_BACKEND
    if _ACTIVE_BACKEND is not None:
        try:
            _ACTIVE_BACKEND.stop_device()
        except Exception:
            pass
    from utils import adb_utils
    for cmd in (
        "touch /data/local/tmp/trace_capture/stop 2>/dev/null; "
        "echo 0 > /sys/kernel/tracing/tracing_on 2>/dev/null; true",
        "pkill -x tasktime 2>/dev/null; true",
    ):
        try:
            adb_utils.adb_shell_root(serial, cmd, timeout_s=15, check=False)
        except Exception:
            pass


def send_stop(serial: str):
    """外部请求停止：touch 设备端 STOP 文件 + 清理采样设施。"""
    print(f"[stop] requesting stop of experiment on {serial}", file=sys.stderr)
    device_cleanup(serial)
    print("[stop] sent; host-side experiment process will finish teardown",
          file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    ensure_adb_works()
    out_dir = Path(args.out_dir) if args.out_dir else Path(
        f"/tmp/experiment_{time.strftime('%Y%m%d_%H%M%S')}")

    if args.stop:
        send_stop(args.serial)
        return 0

    stop_event = threading.Event()

    def _handler(sig, frame):
        print("\n[stopping]")
        stop_event.set()
        threading.Thread(target=device_cleanup, args=(args.serial,),
                         daemon=True).start()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

    try:
        manifest = run_one_device(args.serial, out_dir, args, stop_event)
    except Exception:
        device_cleanup(args.serial)
        raise
    print(f"[{manifest['serial']}] done. out_dir={out_dir} "
          f"samples={manifest.get('samples', 0)} "
          f"errors={manifest.get('sample_errors', 0)}")
    return 130 if stop_event.is_set() else 0


if __name__ == "__main__":
    sys.exit(main())
