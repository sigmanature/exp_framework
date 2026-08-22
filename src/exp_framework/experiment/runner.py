"""实验前端统一入口：prepare -> sample_start -> backend.run -> sample_end。

用法：
  python3 -m experiment.runner --serial <s> --from-config config/baseline_4k_config.json
  python3 -m experiment.runner --serial <s> --from-config <cfg> --stop

信号（SIGINT/SIGTERM）：置位 stop_event + 启动设备清理线程；
backend.run() 会尽快退出，sample_end 在 finally 中必然执行。
"""
import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from exp_framework.utils.adb_utils import ensure_adb_works
from exp_framework.utils.exp_lock import (exp_lock_claim, exp_lock_heartbeat,
                                          exp_lock_mark_cleanup_failed,
                                          exp_lock_release)

import exp_framework.backend  # noqa: F401  (注册后端副作用)
from exp_framework.experiment.experiment import create_experiment
from exp_framework.experiment.config import (load_config, backend_from_config,
                               new_run_manifest, write_run_manifest,
                               resolve_sample_config)
from exp_framework.experiment.sample import sample_start, sample_end


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


def _generate_exp_id(input_cfg: Dict[str, Any], args: Any) -> str:
    """exp_id = [exp_name_]%Y%m%d_%H%M%S（秒级，机制保证唯一）。

    优先级：exp_ctx.exp_name（manifest）→ --exp-name（CLI 覆盖）→ 无前缀。
    exp_name 仅允许 [A-Za-z0-9_-]，防路径注入/特殊字符。
    """
    ctx = (input_cfg.get("config", {}) or {}).get("exp_ctx", {}) or {}
    exp_name = str(ctx.get("exp_name") or "") or str(getattr(args, "exp_name", "") or "")
    exp_name = re.sub(r"[^A-Za-z0-9_-]", "", exp_name)[:64]
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"{exp_name}_{ts}" if exp_name else ts


def run_with_config(serial: str, out_dir: Path, input_cfg: Dict[str, Any],
                    args: argparse.Namespace,
                    stop_event: threading.Event) -> Dict[str, Any]:
    """按输入 config 跑一次实验（框架主流程）。薄壳可绕过 CLI 直接调用。

    统一流程 = exp_lock 串行化（claim → 心跳 → 实验 → cleanup → 状态机更新）。
    out_dir 参数为 base 目录，实际输出 = base/<exp_id>（exp_id 自动生成）。
    """
    # 0) exp_ctx（实验域/设备/agent 会话）+ exp_id（时间戳唯一）→ 串行锁
    ctx = (input_cfg.get("config", {}) or {}).get("exp_ctx", {}) or {}
    domain = str(ctx.get("domain") or "pixel")
    exp_id = _generate_exp_id(input_cfg, args)
    out_dir = out_dir / exp_id
    out_dir.mkdir(parents=True, exist_ok=True)
    session_id = str(ctx.get("session_id") or os.environ.get("OPENCODE_SESSION_ID", ""))
    agent_tool = str(ctx.get("agent_tool") or os.environ.get("AGENT_TOOL", ""))
    claim = exp_lock_claim(domain, serial, exp_id, str(out_dir),
                           session_id=session_id, agent_tool=agent_tool,
                           enqueue_on_busy=False)
    if not claim.startswith("running"):
        print(f"[{serial}] exp_lock: {claim}", file=sys.stderr)
        raise RuntimeError(
            f"设备 ({domain},{serial}) 已被占用，实验 {exp_id} 不能启动。"
            f"排队/等待请用启动器（exp_lock_poll_until_free），"
            f"或查询 exp_lock_status 确认占用者。")

    # 0b) 心跳线程（60s 刷新；cleanup 完成后停）
    heartbeat_stop = threading.Event()

    def _heartbeat():
        while not heartbeat_stop.wait(60):
            try:
                exp_lock_heartbeat(domain, serial, exp_id)
            except Exception:
                pass

    threading.Thread(target=_heartbeat, name=f"exp_lock_hb_{serial}",
                     daemon=True).start()

    # 1) 探测设备权限模式（root / su，自动选择；失败早停）
    from exp_framework.utils import adb_utils
    adb_utils.ensure_privilege(serial)

    # 2) 输入 config -> 后端参数 + 全局参数 + 采样配置
    name, backend_cfg, global_cfg = backend_from_config(input_cfg)
    manifest = new_run_manifest(serial, input_cfg)
    manifest = _global_overrides(args, manifest)
    manifest["exp_lock"] = {"domain": domain, "exp_id": exp_id,
                            "claim": claim, "session_id": session_id,
                            "agent_tool": agent_tool}
    manifest_path = out_dir / "run_manifest.json"
    write_run_manifest(manifest, manifest_path)

    # 3) 实例化后端（注册为活跃后端，供信号/清理立即停设备端）
    global _ACTIVE_BACKEND
    backend = create_experiment(name, backend_cfg, global_cfg, serial,
                                out_dir, stop_event)
    _ACTIVE_BACKEND = backend

    # 4-6) prepare → sample_start → run；任何失败（含 prepare/sample_start）
    #      都走统一收尾 _finish_with_lock（保证游标状态机自洽）
    sample_cfg: Dict[str, Any] = {}
    sess = None
    run_exc: Optional[BaseException] = None
    try:
        # 4) prepare（设备准备 + 后端预检；失败早停，采样不启动）
        private_fields = backend.prepare()
        if private_fields:
            manifest.update(private_fields)
            write_run_manifest(manifest, manifest_path)

        # 5) sample_start（sample_config = 模板 + manifest 差异 深合并；合成结果写入清单）
        sample_cfg = resolve_sample_config(manifest.get("sample_config", {}))
        manifest["sample_config"] = sample_cfg
        write_run_manifest(manifest, manifest_path)
        resolved_pkgs = list((private_fields or {}).get("packages_resolved", {}).keys())
        sess = sample_start(serial, out_dir, sample_cfg, manifest["config"],
                            args, stop_event, resolved_pkgs=resolved_pkgs)

        # 6) backend.run
        backend_result: Dict[str, Any] = {}
        try:
            backend_result = backend.run() or {}
            if backend_result:
                manifest.update(backend_result)
        except BaseException as exc:
            run_exc = exc
            raise
    except BaseException as exc:
        if run_exc is None:
            run_exc = exc
        raise
    finally:
        _finish_with_lock(serial, out_dir, sample_cfg, manifest["config"],
                          args, sess, backend,
                          domain, exp_id,
                          heartbeat_stop,
                          stopped=stop_event.is_set(),
                          run_exc=run_exc)
        _ACTIVE_BACKEND = None

    # 7) manifest 收尾
    manifest["status"] = "stopped" if stop_event.is_set() else "finished"
    manifest["end_host_ts"] = int(time.time())
    sr = getattr(sess, "sampling_result", {"samples": 0, "errors": 0})
    manifest["samples"] = sr.get("samples", 0)
    manifest["sample_errors"] = sr.get("errors", 0)
    write_run_manifest(manifest, manifest_path)
    return manifest


def _device_residual_check(serial: str) -> List[str]:
    """尽力检查设备端残留（best effort；设备离线视为无法确认→计入错误）。

    检查项：设备端 memstress runner 进程、trace probe、tasktime。
    """
    from exp_framework.utils import adb_utils
    problems: List[str] = []
    probe_cmds = {
        "memstress runner": "ps -A | grep -E 'device_cycle_runner|memstress'",
        "trace probe": "ls /data/local/tmp/trace_capture/ 2>/dev/null | grep -q . && echo RUNNING",
        "tasktime": "ps -A | grep -c tasktime",
    }
    try:
        for label, cmd in probe_cmds.items():
            out = adb_utils.adb_shell_root(serial, cmd, timeout_s=5,
                                           check=False)
            out = (out or "").strip()
            if label == "tasktime":
                if out.isdigit() and int(out) > 0:
                    problems.append(f"tasktime 进程残留 ({out})")
            elif out:
                problems.append(f"{label} 残留: {out[:80]}")
    except Exception:
        problems.append("device offline：无法确认设备端清理状态（需人工检查）")
    return problems


def _device_online(serial: str) -> bool:
    """设备在线探测：adb devices 是否列出该 serial（best effort）。"""
    try:
        import subprocess
        out = subprocess.run(["adb", "devices"], capture_output=True,
                             text=True, timeout=5).stdout
        return serial in out
    except Exception:
        return False


def _finish_with_lock(serial: str, out_dir: Path,
                      sample_cfg: Dict[str, Any],
                      global_cfg: Dict[str, Any], args: Any, sess: Any,
                      backend: Any,
                      domain: str, exp_id: str,
                      heartbeat_stop: threading.Event,
                      stopped: bool, run_exc: Optional[BaseException]) -> None:
    """统一收尾：sample_end → backend.cleanup → 状态机更新。

    状态判定（干净语义）：
    - 设备离线（device_lost 异常或 cleanup 阶段探测离线）→ failed，
      reason 含 device_lost；cleanup 阶段的 adb 失败不叠加（断连本身就是原因）
    - 设备在线但清理失败/残留 → cleanup_failed（锁不放，需人工确认设备干净）
    - 干净 → done | failed（按 stop/异常）
    """
    cleanup_errors: List[str] = []
    device_lost = (
        run_exc is not None and "device lost" in str(run_exc).lower())

    if not device_lost:
        if sess is not None:
            try:
                sample_end(serial, out_dir, sample_cfg, global_cfg, args, sess)
            except Exception as e:
                cleanup_errors.append(f"sample_end: {e}")

        if backend is not None:
            try:
                backend.cleanup()
            except Exception as e:
                cleanup_errors.append(f"backend.cleanup: {e}")

    # 设备在线性探测：离线 → 直接 failed（device_lost），不叠加清理错误
    if not _device_online(serial):
        device_lost = True
        cleanup_errors = []

    if not device_lost:
        cleanup_errors.extend(_device_residual_check(serial))

    heartbeat_stop.set()  # 心跳停止（cleanup 完成后）

    # state/exit_code（experiment_standard 状态机约定）
    state_dir = out_dir / "state"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        if run_exc is not None:
            exit_code = 1
        elif stopped:
            exit_code = 130
        else:
            exit_code = 0
        (state_dir / "exit_code").write_text(str(exit_code) + "\n",
                                             encoding="utf-8")
    except Exception as e:
        if not device_lost:
            cleanup_errors.append(f"state/exit_code 写入: {e}")

    # 状态机更新（游标写入失败不覆盖原异常，只告警）
    try:
        if device_lost:
            reason = f"device_lost: {run_exc if run_exc is not None else 'device offline'}"[:300]
            print(f"[{serial}] exp_lock: release(failed, {reason})", file=sys.stderr)
            exp_lock_release(domain, serial, exp_id, "failed", reason)
        elif cleanup_errors:
            reason = "; ".join(cleanup_errors)[:500]
            print(f"[{serial}] exp_lock: cleanup_failed -> {reason}",
                  file=sys.stderr)
            exp_lock_mark_cleanup_failed(domain, serial, exp_id, reason)
        else:
            state = "failed" if (run_exc is not None or stopped) else "done"
            reason = f"exit_code={exit_code}"
            print(f"[{serial}] exp_lock: release({state})", file=sys.stderr)
            exp_lock_release(domain, serial, exp_id, state, reason)
    except Exception as e:
        print(f"[{serial}] exp_lock update failed (锁状态可能需人工检查): {e}",
              file=sys.stderr)


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
    from exp_framework.utils import adb_utils
    for cmd in (
        "touch /data/local/tmp/trace_capture/stop 2>/dev/null; "
        "echo 0 > /sys/kernel/tracing/tracing_on 2>/dev/null; true",
        "pkill -x tasktime 2>/dev/null; true",
    ):
        try:
            adb_utils.adb_shell_root(serial, cmd, timeout_s=5, check=False)
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
