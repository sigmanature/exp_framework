"""实验前端统一入口：prepare -> sample_start -> backend.run -> sample_end。

用法：
  python3 -m experiment.runner --serial <s> --from-config config/baseline_4k_config.json
  python3 -m experiment.runner --serial <s> --from-config <cfg> --stop

信号（SIGINT/SIGTERM）：置位 stop_event + 启动设备清理线程；
backend.run() 会尽快退出，sample_end 在 finally 中必然执行。
"""
import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from exp_framework.utils.adb_utils import ensure_adb_works
from exp_framework.utils.device_prep import ensure_zram, reboot_device_and_wait
from exp_framework.utils.exp_lock import (ExpLockHeartbeat, busy_message,
                                          exp_lock_claim,
                                          exp_lock_mark_cleanup_failed,
                                          exp_lock_release)
from exp_framework.utils.kernel_boot_utils import verify as kernel_boot_verify
from exp_framework.utils.sysctl_util import verify as sysctl_verify

import exp_framework.backend  # noqa: F401  (注册后端副作用)
from exp_framework.experiment.experiment import create_experiment
from exp_framework.experiment.config import (load_config, backend_from_config,
                               new_run_manifest, write_run_manifest,
                               resolve_precondition_mode, resolve_rounds,
                               resolve_sample_config)
from exp_framework.experiment.sample import (sample_cleanup_remote,
                                             sample_device_residuals,
                                             sample_end, sample_start)


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
    # ---- 多轮次 / precondition 编排（config 文件优先，CLI 仅显式覆盖）----
    p.add_argument("--rounds", type=int, default=None,
                   help="轮次数（重复整个 prepare 后实验多少次；默认取 "
                        "config.config.rounds，缺省为 1）")
    p.add_argument("--precondition-mode", default=None,
                   choices=["once", "per_round", "none"],
                   help="precondition 调度模式（覆盖 config.config.precondition.mode）："
                        "once=所有轮次只执行一次；per_round=每轮开始前执行；"
                        "none=禁用")
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
    if args.rounds is not None:
        cfg["rounds"] = args.rounds
    if args.precondition_mode is not None:
        cfg.setdefault("precondition", {})["mode"] = args.precondition_mode
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


def _apply_sysctl_verify(serial: str, global_cfg: Dict[str, Any]) -> None:
    """框架级内核参数设置+自检（所有后端通用；配置未要求则 noop）。

    sysctl_nodes：设置+回读自检，MISMATCH 即 fail-fast。
    zram：sysctl_nodes 含 zram 节点时确保 swap 启用。
    """
    sysctl_nodes = global_cfg.get("sysctl_nodes") or []
    if not sysctl_nodes:
        return
    sysctl_results = sysctl_verify(global_cfg)
    bad = [r["param"] for r in sysctl_results if not r["ok"]]
    if bad:
        raise RuntimeError(f"sysctl verify failed: {bad}")
    if any("zram" in n.get("path", "") for n in sysctl_nodes):
        ensure_zram(serial)


def _round_needs_precondition(precond_mode: Optional[str],
                              round_no: int) -> bool:
    """本轮是否执行 precondition（重启+打碎）：
    per_round = 每轮；once = 仅第 1 轮。"""
    if precond_mode == "per_round":
        return True
    if precond_mode == "once":
        return round_no == 1
    return False


def _verify_vmstat_keys_present(serial: str,
                                sample_cfg: Dict[str, Any]) -> List[str]:
    """vmstat keys 存在性预检（每轮采样前；vmstat probe 未启用则 noop）。

    与 sysctl 自检对齐：启动日志打印 keys 存在数与缺失键，缺失不中止
    （设备缺键只导致该列恒 0/缺失），但必须可见。
    """
    from exp_framework.utils.cycle_sample import vmstat_probe_keys
    keys = vmstat_probe_keys(sample_cfg)
    if not keys:
        return []
    try:
        from exp_framework.utils import adb_utils
        out = adb_utils.adb_shell_root(
            serial, "cat /proc/vmstat", timeout_s=15, check=False) or ""
        dev_keys = {ln.split()[0] for ln in out.splitlines()
                    if len(ln.split()) >= 2 and ln.split()[1].lstrip("-").isdigit()}
    except Exception as exc:
        print(f"[{serial}] vmstat keys 预检失败: {exc}", file=sys.stderr,
              flush=True)
        return []
    missing = [k for k in keys if k not in dev_keys]
    print(f"  {'vmstat_keys':<28s} = {len(keys) - len(missing)}/{len(keys)} "
          f"存在 [{'OK' if not missing else 'MISMATCH(缺 ' + str(missing) + ')'}]",
          flush=True)
    return missing


def _run_one_round(serial: str, backend: Any, out_dir: Path,
                   manifest: Dict[str, Any], global_cfg: Dict[str, Any],
                   args: argparse.Namespace, stop_event: threading.Event,
                   round_no: int, total_rounds: int,
                   precond_mode: Optional[str]) -> Tuple[Dict[str, Any], Any]:
    """执行一轮：prepare（每轮冷却/锁频/包解析）→ precondition(重启+打碎，
    按 once/per_round) → sysctl 自检 → sample_start → run → sample_end。

    prepare 是每轮动作：冷却是"轮起点"的温度/状态准备，每轮开始前都要做
    （framework 循环、锁频在重启后重新生效），不是只做一次。
    precondition 在其后：restart 重启会重置设备，重启完 prepare 再冷却锁频，
    然后打碎——碎片态不被后续冷却破坏。
    返回 (round_summary, sess)。任一环节失败抛异常；sample_end 在 finally 中
    必然执行。round_summary 供聚合清单 rounds[] 使用。
    """
    final = {
        "round": round_no, "status": "finished", "samples": 0,
        "sample_errors": 0,
    }
    round_dir = out_dir if total_rounds == 1 else out_dir / f"round_{round_no}"
    round_dir.mkdir(parents=True, exist_ok=True)
    if total_rounds > 1:
        backend.assign_work_dir(round_dir / backend.name)
    round_manifest_path = (out_dir if total_rounds == 1
                           else round_dir) / "run_manifest.json"

    # 1) precondition：本轮需要时先重启（干净系统态，含解锁/settle），
    #    重启必须在 prepare 冷却之前——冷却在重启后的新基线上做才有意义。
    pc = global_cfg.get("precondition") or {}
    need_pc = _round_needs_precondition(precond_mode, round_no)
    if need_pc and pc.get("restart"):
        print(f"[{serial}] round {round_no} precondition: reboot for "
              f"clean system state", file=sys.stderr)
        # 合成编排：重启 → 等上线/boot → 解锁保持亮屏 → settle45s+清理
        reboot_device_and_wait(serial, stop_event=stop_event)

    # 2) prepare：每轮执行（冷却/锁频/系统准备/包解析；后端实现幂等）
    private_fields = backend.prepare() or {}
    if private_fields:
        manifest.update(private_fields)

    # 3) precondition 打碎 hook（重启之后、prepare 冷却之后执行）
    pre_result: Dict[str, Any] = {}
    if need_pc:
        pre_result = backend.precondition() or {}
        manifest.update(pre_result)

    # 4) sysctl 自检：打碎会临时改 kfragd 等参数，采样前必须恢复并自检。
    _apply_sysctl_verify(serial, global_cfg)

    sample_cfg = resolve_sample_config(manifest.get("sample_config", {}))
    _verify_vmstat_keys_present(serial, sample_cfg)
    manifest["sample_config"] = sample_cfg
    manifest["round"] = round_no
    manifest["rounds_total"] = total_rounds
    write_run_manifest(manifest, round_manifest_path)
    # 框架级采样主开关：sample_config.enabled=false（合并后）→ 本轮跳过全部
    # 采样，工作负载照跑；缺省/true = 原行为（老模板不受影响）。
    sampling_enabled = bool(sample_cfg.get("enabled", True))
    if not sampling_enabled:
        manifest["sampling"] = "disabled"
    write_run_manifest(manifest, round_manifest_path)

    sess = None
    try:
        resolved_pkgs = list((manifest.get("packages_resolved") or {}).keys())
        if sampling_enabled:
            sess = sample_start(serial, round_dir, sample_cfg,
                                manifest["config"], args, stop_event,
                                resolved_pkgs=resolved_pkgs)
        else:
            print(f"[{serial}] sample_config.enabled=false -> 采样关闭",
                  file=sys.stderr)
        backend_result = backend.run() or {}
        if backend_result:
            manifest.update(backend_result)
            final.update({"timing": backend_result.get("timing"),
                          "total_cycles": backend_result.get("total_cycles"),
                          "launch_failures":
                              (backend_result.get("launch_failures") or [])[:20]})
    finally:
        if sess is not None:
            try:
                sample_end(serial, round_dir, sample_cfg,
                           manifest["config"], args, sess)
            except Exception as exc:
                print(f"[{serial}] round {round_no} sample_end: {exc}",
                      file=sys.stderr)
                final.setdefault("errors", []).append(f"sample_end: {exc}")

    sr = getattr(sess, "sampling_result", {"samples": 0, "errors": 0}) \
        if sess is not None else {"samples": 0, "errors": 0}
    final["samples"] = sr.get("samples", 0)
    final["sample_errors"] = sr.get("errors", 0)
    manifest["samples"] = final["samples"]
    manifest["sample_errors"] = final["sample_errors"]
    write_run_manifest(manifest, round_manifest_path)
    return final, sess


@contextmanager
def _exp_lock_ctx(serial: str, out_dir: Path, input_cfg: Dict[str, Any],
                  args: argparse.Namespace):
    """exp_lock claim + 心跳线程生命周期管理。

    with 块结束（成功/异常/收尾崩溃）都会停止心跳线程；claim 失败抛 busy。
    yield 字段：domain/exp_id/out_dir(实验根目录)/claim/session_id/
    agent_tool/heartbeat_stop。
    """
    ctx = (input_cfg.get("config", {}) or {}).get("exp_ctx", {}) or {}
    # serial 以 runner 参数为准统一注入 exp_ctx：所有 verify 系列
    # （sysctl/vmstat/trace/pkg/tasktime/kernel_boot）都从 exp_ctx.serial 读设备号，
    # manifest 可能未声明 serial（如 memstress 系 manifest 只有 exp_name/domain），
    # 统一在此补齐——runner 参数是权威，exp_ctx 只是配置容器。
    ctx.setdefault("serial", serial)
    domain = str(ctx.get("domain") or "pixel")
    exp_id = _generate_exp_id(input_cfg, args)
    exp_out_dir = out_dir / exp_id
    exp_out_dir.mkdir(parents=True, exist_ok=True)
    session_id = str(ctx.get("session_id") or os.environ.get("OPENCODE_SESSION_ID", ""))
    agent_tool = str(ctx.get("agent_tool") or os.environ.get("AGENT_TOOL", ""))

    claim = exp_lock_claim(domain, serial, exp_id, str(exp_out_dir),
                           session_id=session_id, agent_tool=agent_tool,
                           enqueue_on_busy=False)
    if not claim.startswith("running"):
        print(f"[{serial}] exp_lock: {claim}", file=sys.stderr)
        raise RuntimeError(busy_message(domain, serial, exp_id))

    # 心跳托管（集中实现：锁定续期 + 连续失败告警；锁已释放自动停跳）
    heartbeat = ExpLockHeartbeat(domain, serial, exp_id)
    try:
        heartbeat.start()
    except Exception as exc:
        try:
            exp_lock_release(domain, serial, exp_id, "failed",
                             f"heartbeat start failed: {exc}")
        except Exception:
            pass
        raise
    try:
        yield {"domain": domain, "exp_id": exp_id, "out_dir": exp_out_dir,
               "claim": claim, "session_id": session_id,
               "agent_tool": agent_tool, "heartbeat": heartbeat}
    finally:
        heartbeat.stop()


def _prepare_stage(serial: str, exp_ctx: Dict[str, Any],
                   manifest: Dict[str, Any], manifest_path: Path,
                   input_cfg: Dict[str, Any], args: argparse.Namespace,
                   stop_event: threading.Event) -> Tuple[Any, Dict[str, Any]]:
    """一次性准备：权限探测 → 配置解析 → 后端实例化 → boot 参数校验。

    backend.prepare() 不在这一层——prepare 是每轮动作（冷却/锁频/包解析），
    由 _run_one_round 每轮调用。manifest 由调用方持有（就地更新）。
    """
    from exp_framework.utils import adb_utils
    adb_utils.ensure_privilege(serial)

    name, backend_cfg, global_cfg = backend_from_config(input_cfg)
    manifest["exp_lock"] = {"domain": exp_ctx["domain"], "exp_id": exp_ctx["exp_id"],
                            "claim": exp_ctx["claim"],
                            "session_id": exp_ctx["session_id"],
                            "agent_tool": exp_ctx["agent_tool"]}
    write_run_manifest(manifest, manifest_path)

    global _ACTIVE_BACKEND
    backend = create_experiment(name, backend_cfg, global_cfg, serial,
                                exp_ctx["out_dir"], stop_event)
    _ACTIVE_BACKEND = backend

    # 框架级 boot 参数校验（boot_params 配置驱动；未配置则 noop）。
    # pixel：回读 /sys/module/kernel/parameters/<param>，MISMATCH 时重打包
    # vendor_boot（保留官方 bootconfig + 注入参数）→ 刷入 → 重启 → 回读。
    # 这是实验开始前的一次性 boot 校验；每轮的设备准备由 prepare 承担。
    boot_params = global_cfg.get("boot_params") or []
    if boot_params:
        boot_results = kernel_boot_verify(global_cfg)
        bad = [r["param"] for r in boot_results if not r["ok"]]
        if bad:
            raise RuntimeError(f"boot_params verify failed: {bad}")
    return backend, global_cfg


def _run_rounds_loop(serial: str, backend: Any, manifest: Dict[str, Any],
                     global_cfg: Dict[str, Any], out_dir: Path,
                     manifest_path: Path, args: argparse.Namespace,
                     stop_event: threading.Event, rounds: int,
                     precond_mode: Optional[str]) -> Any:
    """轮次循环：每轮 precondition(per_round) → sysctl 自检 → 采样 → run → 采样收尾。

    就地更新 manifest（rounds[]/samples/sample_errors/rounds_done），
    返回最后一轮 SampleSession（供 stop_reason 组装）；rounds=1 即旧行为。
    """
    rounds_summary: List[Dict[str, Any]] = []
    sess = None
    for r in range(1, rounds + 1):
        if stop_event.is_set():
            print(f"[{serial}] stop requested before round {r} "
                  f"({r - 1}/{rounds} done)", file=sys.stderr)
            break
        round_summary, sess = _run_one_round(
            serial=serial, backend=backend, out_dir=out_dir,
            manifest=manifest, global_cfg=global_cfg, args=args,
            stop_event=stop_event, round_no=r, total_rounds=rounds,
            precond_mode=precond_mode)
        rounds_summary.append(round_summary)
        manifest["rounds"] = rounds_summary
        manifest["samples"] = sum(s["samples"] for s in rounds_summary)
        manifest["sample_errors"] = sum(
            s["sample_errors"] for s in rounds_summary)
        print(f"[{serial}] round {r}/{rounds} done: "
              f"samples={round_summary['samples']} "
              f"samples_errors={round_summary['sample_errors']}",
              file=sys.stderr)
    manifest["rounds_done"] = len(rounds_summary)
    write_run_manifest(manifest, manifest_path)
    return sess


def run_with_config(serial: str, out_dir: Path, input_cfg: Dict[str, Any],
                    args: argparse.Namespace,
                    stop_event: threading.Event) -> Dict[str, Any]:
    """按输入 config 跑实验（框架主流程）。薄壳可绕过 CLI 直接调用。

    统一流程 = exp_lock 串行化（claim → 心跳 → 准备阶段（权限/解析/boot校验/
    prepare/once 打碎）→ 轮次阶段（每轮: per_round 打碎 → sysctl 自检 →
    采样 → run → 采样收尾）→ _finish_with_lock 统一收尾（清理/残留检查/
    exit_code/锁状态机/manifest 终态））。
    out_dir 参数为 base 目录，实际输出 = base/<exp_id>（exp_id 自动生成）。

    轮次（config.config.rounds，默认 1）：rounds=1 保持旧布局（无 round_* 子目录）；
    rounds>1 时每轮产物落入 base/<exp_id>/round_<n>/，根目录是聚合清单。
    precondition 两种模式（config.config.precondition.mode）：
    once=只在第一轮前执行一次；per_round=每轮开始前执行。无块则不执行。
    任何终态（正常/异常/用户停止）都由 _finish_with_lock 统一收尾，
    manifest 终态（status/stop_reason/end_host_ts）也由它写出。
    """
    _, _, global_cfg = backend_from_config(input_cfg)
    rounds = resolve_rounds(global_cfg, getattr(args, "rounds", None))
    precond_mode = resolve_precondition_mode(
        global_cfg, getattr(args, "precondition_mode", None))

    global _ACTIVE_BACKEND
    backend = None
    sess = None
    manifest = _global_overrides(args, new_run_manifest(serial, input_cfg))
    run_exc: Optional[BaseException] = None
    with _exp_lock_ctx(serial, out_dir, input_cfg, args) as exp_ctx:
        manifest_path = exp_ctx["out_dir"] / "run_manifest.json"
        try:
            backend, global_cfg = _prepare_stage(
                serial, exp_ctx, manifest, manifest_path, input_cfg, args,
                stop_event)
            sess = _run_rounds_loop(
                serial, backend, manifest, global_cfg, exp_ctx["out_dir"],
                manifest_path, args, stop_event, rounds, precond_mode)
        except BaseException as exc:
            run_exc = exc
            raise
        finally:
            # _prepare_stage 中 create_experiment 之后若抛异常，局部 backend
            # 仍是 None，但 _ACTIVE_BACKEND 已持有实例——兜底传给收尾执行 cleanup
            final_backend = backend if backend is not None else _ACTIVE_BACKEND
            try:
                _finish_with_lock(serial, exp_ctx["out_dir"], sess,
                                  final_backend,
                                  exp_ctx["domain"], exp_ctx["exp_id"],
                                  exp_ctx["heartbeat"],
                                  manifest, manifest_path,
                                  stopped=stop_event.is_set(),
                                  run_exc=run_exc)
            except BaseException as exc2:
                # 最后兜底：收尾函数自身崩溃（参数错误/内部异常）也必须释放锁，
                # 否则游标卡 running 导致设备永久不可用（只能人工 clean）。
                print(f"[{serial}] _finish_with_lock failed: {exc2}",
                      file=sys.stderr)
                try:
                    exp_lock_release(exp_ctx["domain"], serial,
                                     exp_ctx["exp_id"], "failed",
                                     f"finish_with_lock crashed: {exc2}")
                    print(f"[{serial}] exp_lock: forced release(failed)",
                          file=sys.stderr)
                except Exception as exc3:
                    print(f"[{serial}] forced exp_lock release failed, "
                          f"需人工 exp_lock_clean: {exc3}", file=sys.stderr)
            _ACTIVE_BACKEND = None
    return manifest


def _device_residual_check(serial: str, backend: Any = None) -> List[str]:
    """设备端残留检查（best effort；设备离线视为无法确认→计入错误）。

    各层各查各的：
    - sample 层：trace probe / tasktime（sample_device_residuals）
    - backend 层：自己部署的东西（如 memstress 的 device runner，
      Experiment.device_residuals）
    框架不展开具体工具名——只做合并与离线还原。
    """
    problems: List[str] = []
    try:
        problems.extend(sample_device_residuals(serial))
    except Exception as exc:
        problems.append(f"sample residual check failed: {exc}")
    if backend is not None:
        try:
            problems.extend(backend.device_residuals(serial) or [])
        except Exception as exc:
            problems.append(f"backend residual check failed: {exc}")
    return problems


def _device_online(serial: str) -> bool:
    """设备在线探测：adb devices 是否列出该 serial（best effort）。"""
    try:
        from exp_framework.utils import adb_utils
        return serial in adb_utils.adb_devices()
    except Exception:
        return False


def _finish_with_lock(serial: str, out_dir: Path,
                      sess: Any, backend: Any,
                      domain: str, exp_id: str,
                      heartbeat: ExpLockHeartbeat,
                      manifest: Dict[str, Any], manifest_path: Path,
                      stopped: bool,
                      run_exc: Optional[BaseException]) -> None:
    """统一收尾：backend.cleanup → 设备残留检查 → 状态机更新 → manifest 终态。

    所有终态（正常/异常/用户停止/设备丢失/清理失败）的唯一出口：
    锁状态机（done/failed/cleanup_failed）与 manifest 终态
    （status/end_host_ts/stop_reason）都只在这里写，不再散落。
    sample_end 已在每轮 _run_one_round 的 finally 中执行，这里不收采样
    （避免对同一 sess 二次收尾）。sess 仅用于 stop_reason 组装。

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
        cleanup_errors.extend(_device_residual_check(serial, backend))

    heartbeat.stop()  # 心跳停止（cleanup 完成后）

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
    stop_reason = stop_reason_summary(sess, run_exc)
    if stop_reason.get("triggered_by"):
        print(f"[{serial}] stop_reason: {stop_reason}", file=sys.stderr)
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
            if stopped and stop_reason.get("triggered_by"):
                reason += f"; stop_reason={stop_reason['triggered_by']}({stop_reason.get('detail', '')})"
            print(f"[{serial}] exp_lock: release({state})", file=sys.stderr)
            exp_lock_release(domain, serial, exp_id, state, reason)
    except Exception as e:
        print(f"[{serial}] exp_lock update failed (锁状态可能需人工检查): {e}",
              file=sys.stderr)

    # manifest 终态（status/end_host_ts/stop_reason）：终态唯一出口，
    # run_with_config 的旧"except分支+step6"两处重复写已删除。
    try:
        if manifest:
            manifest["status"] = ("failed" if run_exc is not None
                                  else "stopped" if stopped else "finished")
            manifest["end_host_ts"] = int(time.time())
            manifest["stop_reason"] = stop_reason
            write_run_manifest(manifest, manifest_path)
    except Exception as e:
        print(f"[{serial}] manifest final status write failed: {e}",
              file=sys.stderr)

    # stop_reason 写入实验目录（供 manifest 收尾引用/审计）
    try:
        (out_dir / "state" / "stop_reason.json").write_text(
            json.dumps(stop_reason, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    except Exception:
        pass


def run_one_device(serial: str, out_dir: Path, args: argparse.Namespace,
                   stop_event: threading.Event) -> Dict[str, Any]:
    """CLI 入口版：从 --from-config / --backend 组装输入 config。"""
    input_cfg = _load_input_config(args)
    return run_with_config(serial, out_dir, input_cfg, args, stop_event)


# ---------------- 信号 / 清理 ----------------

_ACTIVE_BACKEND = None  # 当前运行的后端（供信号/清理时调用 stop_device）

# ---- stop 原因登记器：stop_event 被置位时记录来源，收尾透传到 manifest/游标 ----
_STOP_REASON: Dict[str, Any] = {}


def record_stop_reason(key: str, detail: Any = "") -> None:
    """登记 stop 来源（信号/crash/device_lost 等），线程安全。

    收尾时 _finish_with_lock 读取并写入 manifest["stop_reason"] 与游标 reason，
    解决"实验完成但退出码异常/失败原因不明"的透传问题。
    """
    _STOP_REASON.clear()
    _STOP_REASON["triggered_by"] = key
    _STOP_REASON["detail"] = str(detail)[:200]
    _STOP_REASON["ts"] = int(time.time())


def _signal_name(sig) -> str:
    try:
        return signal.Signals(sig).name
    except Exception:
        return str(sig)


def stop_reason_summary(sess: Any, run_exc: Optional[BaseException]) -> Dict[str, Any]:
    """组装 stop 原因：信号登记（handler 写入）+ crash 检测 + 异常。"""
    info = dict(_STOP_REASON)
    if not info.get("triggered_by") and sess is not None:
        if getattr(sess, "crash_event", None) is not None and sess.crash_event.is_set():
            info = {"triggered_by": "crash_detected",
                    "detail": "logcat 命中 crash 签名（combined_stop 置位）",
                    "ts": int(time.time())}
    if run_exc is not None:
        info.setdefault("run_exc", f"{type(run_exc).__name__}: {run_exc}"[:200])
    return info


def device_cleanup(serial: str):
    """框架统一设备清理：后端设备端停止 + 采样设施清理（均委托各层）。

    - 后端：active backend.stop_device()（memstress 等自己懂自己的 runner）
    - 采样：sample_cleanup_remote（trace probe / tracing / tasktime）
    框架不展开具体工具命令——各层各管各的设备端东西。
    """
    global _ACTIVE_BACKEND
    if _ACTIVE_BACKEND is not None:
        try:
            _ACTIVE_BACKEND.stop_device()
        except Exception:
            pass
    sample_cleanup_remote(serial)


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
        record_stop_reason("signal", f"{_signal_name(sig)} (sig={sig})")
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
