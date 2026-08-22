"""采样前端：sample_start / sample_end。

由 sample_config 驱动，与实验后端无关：
- sample_start：基线快照（vmstat/odpm/lock_stat）+ 采样线程 + crash/logcat +
  tasktime/trace 部署启动（在 prepare 之后调用）
- sample_end：全部采样设施停止与产物推导（trace stop/tasktime finish/
  vmstat_end/odpm delta/lock_stat delta/derive）

设备准备（唤醒/锁频/冷却）不在这里 —— 属于 Experiment.prepare()。
"""
import json
from dataclasses import dataclass
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Sequence
from exp_framework.experiment.config import load_default_sample_config
from exp_framework.utils.adb_utils import adb_shell, start_logcat_stream
from exp_framework.utils import adb_utils
from exp_framework.utils.crash_signature import TargetCrashSignatureDetector
from exp_framework.utils.cycle_sample import start_cycle_samplers
from exp_framework.utils.lockstat_utils import (capture_lock_stat, lock_stat_delta,
                                  read_lock_stat)
from exp_framework.utils.sampling_utils import (run_derive_metrics,)
from exp_framework.utils.trace_utils import (validate_events, deploy_trace_probe,
                               start_trace_probe, stop_trace_probe)
from exp_framework.utils.vmstat_utils import (derive_vmstat_csv, read_vmstat)

_TASKTIME_DEV = "/data/local/tmp/tasktime"
_TASKTIME_OUT = "/data/local/tmp/tasktime_out.txt"


# ---------------- tasktime（设备端 CPU 时间采样）----------------

@dataclass
class TasktimeTarget:
    """要采样的进程；pid 为 None 表示设备上不存在（跳过）。"""
    name: str
    pid: Optional[int] = None


def _tasktime_adb(serial: str, cmd: str, timeout_s: int = 30,
                  retries: int = 3) -> str:
    last = ""
    for attempt in range(max(1, retries)):
        cp = subprocess.run(["adb", "-s", serial, "shell", cmd],
                            capture_output=True, text=True, timeout=timeout_s)
        last = (cp.stdout or "") + (cp.stderr or "")
        if cp.returncode == 0 and last.strip():
            return last
        time.sleep(1 + attempt)
    return last


def _tasktime_adb_root(serial: str, cmd: str, timeout_s: int = 30,
                       retries: int = 3) -> str:
    """tasktime 用权限命令（root/su 自动）。"""
    last = ""
    for attempt in range(max(1, retries)):
        try:
            out = adb_utils.adb_shell_root(serial, cmd, timeout_s=timeout_s,
                                           check=False)
        except Exception as e:
            last = str(e)
            time.sleep(1 + attempt)
            continue
        if out.strip():
            return out
        last = out
        time.sleep(1 + attempt)
    return last


def _push_tasktime(serial: str):
    local = os.environ.get("TASKTIME_BIN", "/home/nzzhao/下载/tasktime")
    if not os.path.exists(local):
        print(f"[tasktime] local binary not found: {local}", file=sys.stderr)
        return
    subprocess.run(["adb", "-s", serial, "push", local, _TASKTIME_DEV],
                   capture_output=True, timeout=60)
    _tasktime_adb(serial, f"chmod 755 {_TASKTIME_DEV}")


def resolve_tasktime_targets(serial: str, proc_names: Sequence[str],
                             strict: bool = True,
                             retries: int = 3) -> List[TasktimeTarget]:
    """把进程名解析成 PID（pgrep -x）。

    strict=True：解析失败抛 RuntimeError 中止实验；strict=False 优雅跳过。
    """
    targets: List[TasktimeTarget] = []
    for name in proc_names:
        raw = _tasktime_adb_root(serial, f"pgrep -x {name} | head -1",
                                 retries=retries)
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if lines and lines[0].isdigit():
            targets.append(TasktimeTarget(name=name, pid=int(lines[0])))
        else:
            diag: Dict[str, str] = {}
            for label, cmd in (
                ("plain", f"pgrep -x {name}"),
                ("shc", f'sh -c "pgrep -x {name} | head -1"'),
                ("pidof", f"pidof {name}"),
            ):
                diag[label] = f"cmd={cmd!r} -> out={_tasktime_adb_root(serial, cmd, retries=1)!r}"
            if strict:
                raise RuntimeError(
                    f"tasktime target '{name}' not resolvable on device "
                    f"(raw={raw!r}; diag={diag}); aborting")
            print(f"[tasktime] resolve {name} failed: raw={raw!r} diag={diag}",
                  file=sys.stderr)
            targets.append(TasktimeTarget(name=name, pid=None))
    return targets


def _start_tasktime(serial: str, pids: str):
    cmd = (f"rm -f {_TASKTIME_OUT}; "
           f"({_TASKTIME_DEV} -p {pids} 2 0 > {_TASKTIME_OUT} 2>&1 &)")
    _tasktime_adb_root(serial, cmd)


def _finish_tasktime(serial: str,
                     targets: Sequence[TasktimeTarget] = ()) -> str:
    _tasktime_adb_root(serial, f"pkill -f {_TASKTIME_DEV}", timeout_s=15)
    time.sleep(1)
    out = _tasktime_adb_root(serial, f"cat {_TASKTIME_OUT} 2>/dev/null",
                             timeout_s=30)
    if not out.strip():
        return ""
    blocks = [b for b in out.split("\n\n") if b.strip()]
    if len(blocks) < 2:
        return f"tasktime: insufficient samples ({len(blocks)} block(s))\n{out[:2000]}"
    name_by_pid = {t.pid: t.name for t in targets if t.pid is not None}
    lines = []
    pid_first: Dict[str, str] = {}
    pid_last: Dict[str, str] = {}
    rec_first: Dict[str, List[str]] = {}
    rec_last: Dict[str, List[str]] = {}
    for block in blocks:
        for ln in block.splitlines():
            parts = ln.split()
            if not parts:
                continue
            if parts[0].isdigit() and len(parts) >= 2:
                pid_first.setdefault(parts[0], parts[1])
                pid_last[parts[0]] = parts[1]
            elif parts[0] in ("direct", "memcg", "node") and len(parts) >= 5:
                vals = parts[1:5]
                rec_first.setdefault(parts[0], vals)
                rec_last[parts[0]] = vals

    lines.append("tasktime deltas (experiment window, first vs last sample)")
    lines.append("PID   running_ms_delta   name")
    for pid in sorted(set(pid_first) | set(pid_last), key=int):
        fv = float(pid_first.get(pid, "0.0"))
        lv = float(pid_last.get(pid, "0.0"))
        name = name_by_pid.get(int(pid), "?")
        lines.append(f"{pid}   {lv - fv:.3f}   {name}")
    lines.append("")
    lines.append("reclaim (system-wide) delta: count  total_ms  avg_us  max_us")
    for kind in ("direct", "memcg", "node"):
        fr = rec_first.get(kind)
        lr = rec_last.get(kind)
        if fr and lr:
            try:
                d = [float(lr[i]) - float(fr[i]) for i in range(4)]
                lines.append(f"{kind:8s} {int(d[0]):>6} {d[1]:>10.3f} "
                             f"{d[2]:>10.3f} {d[3]:>10.3f}")
            except (ValueError, IndexError):
                lines.append(f"{kind}: parse error")
    return "\n".join(lines)


# ---------------- 通用采样设施 ----------------

class StopEvent:
    """多线程停止信号，接口与 threading.Event 兼容（含 wait）。"""
    def __init__(self):
        self._events: List[threading.Event] = []

    def add(self, e: threading.Event):
        self._events.append(e)

    def set(self):
        for e in self._events:
            e.set()

    def is_set(self):
        return any(e.is_set() for e in self._events)

    def wait(self, timeout: Optional[float] = None) -> bool:
        """等待任一子事件置位（返回 True）或超时（返回 False）。

        多 Event 无法 select，用 50ms 轮询切片实现，满足事件化唤醒粒度。
        """
        if timeout is not None and timeout <= 0:
            return self.is_set()
        deadline = (time.monotonic() + timeout
                    if timeout is not None else None)
        while True:
            if self.is_set():
                return True
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.05, remaining))
            else:
                time.sleep(0.05)


def ensure_network(serial: str):
    while True:
        cp = subprocess.run(
            ["adb", "-s", serial, "shell",
             "ping -c 1 -W 2 8.8.8.8 > /dev/null 2>&1 && echo online || echo offline"],
            capture_output=True, text=True, timeout=15)
        if cp.stdout and "online" in cp.stdout:
            print(f"[{serial}] network OK")
            return
        print(f"[{serial}] 设备未联网，请连接 WiFi 后继续...", file=sys.stderr)
        time.sleep(5)


def record_vmstat_start(serial: str, out_dir: Path,
                        keys: Optional[Sequence[str]] = None) -> dict:
    """记录实验开始前的 /proc/vmstat 到 vmstat_start.json。"""
    values = read_vmstat(serial, keys=keys)
    (out_dir / "vmstat_start.json").write_text(
        json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return values


def record_vmstat_end(serial: str, out_dir: Path,
                      keys: Optional[Sequence[str]] = None) -> dict:
    """记录实验结束后的 /proc/vmstat 到 vmstat_end.json。"""
    values = read_vmstat(serial, keys=keys)
    (out_dir / "vmstat_end.json").write_text(
        json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return values


# --------------- ODPM（Pixel 硬件能耗计）---------------

_ODPM_RAIL = re.compile(r"^\s*(CPUCL\d+|GPU)\s+:\s+([0-9.]+)\s+mWs$")


def capture_odpm(serial: str, out_dir: Path, tag: str) -> float:
    """抓 PowerStats HAL 能耗计 -> odpm_<tag>.txt；返回抓取时刻设备
    CLOCK_MONOTONIC（秒），用于精确计算采样窗口。"""
    remote = f"/data/local/tmp/odpm_{tag}.txt"
    try:
        adb_utils.adb_shell_root(
            serial,
            f"dumpsys android.hardware.power.stats.IPowerStats/default "
            f"| sed -n '/energy consumers/,/^=============/p' "
            f"> {remote}; awk '{{print $1}}' /proc/uptime > {remote}.up",
            timeout_s=60, check=False)
        subprocess.run(["adb", "-s", serial, "pull", remote,
                        str(out_dir / f"odpm_{tag}.txt")],
                       capture_output=True, timeout=120)
        subprocess.run(["adb", "-s", serial, "pull", remote + ".up",
                        str(out_dir / f"odpm_{tag}_uptime.txt")],
                       capture_output=True, timeout=30)
        up = (out_dir / f"odpm_{tag}_uptime.txt").read_text(
            encoding="utf-8", errors="ignore").strip()
        return float(up.split()[0]) if up else 0.0
    except Exception:
        return 0.0


def parse_odpm(path: Path) -> dict:
    """顶层能耗轨道（CPUCL0/1/2, GPU）-> {rail: mWs}。"""
    out: dict = {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    if "energy consumers" not in text:
        return out
    body = text.split("energy consumers", 1)[1]
    for line in body.splitlines():
        m = _ODPM_RAIL.match(line)
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


def odpm_delta(start_path: Path, end_path: Path, duration_s: float) -> dict:
    """每轨道平均功率：(end - start) / 窗口时长（mW）+ 能耗差值（mWs）。"""
    s = parse_odpm(start_path)
    e = parse_odpm(end_path)
    delta: dict = {"duration_s": round(duration_s, 3)}
    total_mws = 0.0
    for rail in sorted(set(s) | set(e)):
        d_mws = e.get(rail, 0.0) - s.get(rail, 0.0)
        if d_mws < 0:
            d_mws = 0.0  # 计数器重置：视为无数据
        delta[f"{rail}_energy_mws"] = round(d_mws, 3)
        delta[f"{rail}_avg_mw"] = round(d_mws / duration_s, 3)
        total_mws += d_mws
    delta["total_energy_mws"] = round(total_mws, 3)
    delta["total_avg_mw"] = round(total_mws / duration_s, 3)
    return delta


# ---------------- sample_start / sample_end ----------------

class SampleSession:
    """sample_start 创建的句柄集合，交给 sample_end 收尾。"""
    def __init__(self):
        self.sampler_thread: Optional[threading.Thread] = None
        self.threads: List[threading.Thread] = []
        self.sampling_result = {"samples": 0, "errors": 0}
        self.combined_stop: Optional[StopEvent] = None
        self.logcat_handle = None
        self.vmstat_keys = None
        self.odpm_enabled = False
        self.odpm_start_uptime = 0.0
        self.lock_stat_enabled = False
        self.lock_stat_start_text = ""
        self.trace_captures: List[dict] = []
        self.tasktime_targets: List[TasktimeTarget] = []
        self.crash_event = threading.Event()


def sample_start(serial: str, out_dir: Path, sample_cfg: Dict[str, Any],
                 global_cfg: Dict[str, Any], args: Any,
                 stop_event: threading.Event,
                 resolved_pkgs: Optional[List[str]] = None) -> SampleSession:
    """启动全部采样设施。必须在后端 prepare() 之后调用。

    global_cfg：config["config"]（全局/采样参数，含 CLI 覆盖）。
    resolved_pkgs：后端 prepare 解析出的包名（用于 crash 检测），可空。
    """
    sess = SampleSession()
    cycle_cfg = sample_cfg.get("cycle_sample", {}) or {}
    vmstat_cfg = cycle_cfg.get("vmstat", {}) or {}
    sess.vmstat_keys = vmstat_cfg.get("keys") or None
    lock_stat_enabled = bool(sample_cfg.get("lock_stat", {}).get("enabled", False))
    odpm_enabled = bool(sample_cfg.get("power", {}).get("odpm", False))
    trace_captures = list(sample_cfg.get("trace", {}).get("captures") or [])
    tasktime_procs = [p.strip() for p in
                      (sample_cfg.get("tasktime", {}).get("procs") or []) if p]
    sess.trace_captures = trace_captures
    sess.odpm_enabled = odpm_enabled
    sess.lock_stat_enabled = lock_stat_enabled

    # ---- 预校验（设备准备之后、采样启动前，快速失败）----
    tasktime_targets: List[TasktimeTarget] = []
    if tasktime_procs:
        tasktime_targets = resolve_tasktime_targets(
            serial, tasktime_procs,
            strict=bool(sample_cfg.get("tasktime", {}).get("strict", True)))
        print(f"[{serial}] tasktime targets: "
              + ", ".join(f"{t.name}={t.pid}" for t in tasktime_targets),
              file=sys.stderr)
        present = [t for t in tasktime_targets if t.pid is not None]
        absent = [t.name for t in tasktime_targets if t.pid is None]
        if absent:
            print(f"[{serial}] tasktime: absent (skipped): {', '.join(absent)}",
                  file=sys.stderr)
        if present:
            _push_tasktime(serial)
            _start_tasktime(serial, ",".join(str(t.pid) for t in present))
            print(f"[{serial}] tasktime started: "
                  f"{', '.join(f'{t.name}({t.pid})' for t in present)}",
                  file=sys.stderr)
        else:
            print(f"[{serial}] tasktime: no traceable targets, sampler disabled",
                  file=sys.stderr)
    sess.tasktime_targets = tasktime_targets

    # ---- trace probe: deploy + configure + start（device-side readers）----
    if trace_captures:
        deploy_trace_probe(serial, trace_captures)
        start_trace_probe(serial, trace_captures)
        print(f"[{serial}] trace probe started: "
              f"{', '.join((c.get('name') or 'main') for c in trace_captures)}",
              file=sys.stderr)

    if trace_captures:
        for cap in trace_captures:
            missing = validate_events(
                serial, cap.get("events", []),
                strict=bool(sample_cfg.get("trace", {}).get("strict", True)))
            if missing:
                print(f"[{serial}] trace probe: skipping missing events: {missing}",
                      file=sys.stderr)

    if lock_stat_enabled:
        try:
            adb_utils.adb_shell_root(serial, "echo 1 > /proc/sys/kernel/lock_stat",
                                     timeout_s=15, check=False)
        except Exception:
            pass
        probe = read_lock_stat(serial)
        if not probe.strip():
            raise RuntimeError("lock_stat enabled but /proc/lock_stat is empty "
                               "(not readable even as root)")

    # ---- post-prepare hook（prepare 之后、采样启动前）----
    post_cmd = getattr(args, "post_prepare_cmd", None)
    if post_cmd:
        print(f"[{serial}] post-prepare: {post_cmd}")
        try:
            adb_utils.adb_shell_root(serial, post_cmd, timeout_s=30, check=True)
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"post-prepare hook failed: {error}") from error

    # ---- 基线快照 ----
    record_vmstat_start(serial, out_dir, keys=sess.vmstat_keys)
    if odpm_enabled:
        sess.odpm_start_uptime = capture_odpm(serial, out_dir, "start")
    if lock_stat_enabled:
        try:
            adb_utils.adb_shell_root(serial, "echo 1 > /proc/sys/kernel/lock_stat",
                                     timeout_s=15, check=False)
        except Exception:
            pass
        sess.lock_stat_start_text = capture_lock_stat(
            serial, out_dir / "lock_stat_start.txt")

    # ---- cycle_sample 周期采样（统一管理器，按配置启停）----
    local_stop = threading.Event()
    combined_stop = StopEvent()
    combined_stop.add(stop_event)
    combined_stop.add(local_stop)
    sess.combined_stop = combined_stop

    sess.cycle_sample_result: Dict = {}
    sess.threads.extend(start_cycle_samplers(
        serial, out_dir, cycle_cfg, combined_stop,
        result_sink=sess.cycle_sample_result))

    # ---- crash 检测 + logcat ----
    if not getattr(args, "no_crash_detect", False) and resolved_pkgs:
        detector = TargetCrashSignatureDetector(
            serial=serial, target_packages=list(resolved_pkgs), window_lines=500)

        def _on_logcat_line(line: str):
            if sess.crash_event.is_set():
                return
            payload = detector.process_line(line)
            if payload is not None:
                detector.write_payload(out_dir / "crash_signature.json", payload)
                sess.crash_event.set()
                combined_stop.set()

        sess.logcat_handle = start_logcat_stream(
            serial, out_dir, clear_logcat=bool(getattr(args, "clear_logcat", False)),
            filename=f"logcat_{time.strftime('%Y%m%d_%H%M%S')}.txt",
            line_callback=_on_logcat_line, stop_event=stop_event)

    return sess


def sample_end(serial: str, out_dir: Path, sample_cfg: Dict[str, Any],
               global_cfg: Dict[str, Any], args: Any,
               sess: SampleSession) -> None:
    """停止全部采样设施并推导产物。必须在 backend.run() 之后（含异常路径）。"""

    # 先停采样线程（cycle_sample 各域）：combined_stop 置位后自然退出
    if sess.combined_stop is not None:
        sess.combined_stop.set()
    for t in [sess.sampler_thread] + list(sess.threads):
        if t is not None:
            t.join(timeout=10)

    # counters 采样结果回写（原始 (num, err) 或 None）
    counters_result = getattr(sess, "cycle_sample_result", {}).get("counters")
    if isinstance(counters_result, tuple) and len(counters_result) == 2:
        sess.sampling_result = {"samples": counters_result[0],
                                "errors": counters_result[1]}

    if sess.logcat_handle:
        try:
            sess.logcat_handle.stop()
        except Exception:
            pass

    # trace probe stop + pull
    if sess.trace_captures:
        try:
            stop_trace_probe(serial, sess.trace_captures, out_dir)
            print(f"[{serial}] trace probe stopped; outputs under {out_dir}",
                  file=sys.stderr)
        except Exception as error:
            print(f"[{serial}] trace probe stop failed: {error}", file=sys.stderr)

    # tasktime finish + delta
    if sess.tasktime_targets:
        try:
            report = _finish_tasktime(serial, sess.tasktime_targets)
            if report:
                (out_dir / "tasktime_report.txt").write_text(
                    report + "\n", encoding="utf-8")
                print(f"[{serial}] tasktime deltas saved to {out_dir / 'tasktime_report.txt'}",
                      file=sys.stderr)
        except Exception as error:
            print(f"[{serial}] tasktime finish failed: {error}", file=sys.stderr)

    # post-workload hook（收尾上下文：失败只告警，不中断后续收尾）
    post_wl_cmd = getattr(args, "post_workload_cmd", None)
    if post_wl_cmd:
        print(f"[{serial}] post-workload: {post_wl_cmd}")
        try:
            adb_utils.adb_shell_root(serial, post_wl_cmd, timeout_s=30, check=True)
        except Exception as error:
            print(f"[{serial}] post-workload hook failed: {error}", file=sys.stderr)

    # kill fragmem if it was started (--precondition)
    if getattr(args, "precondition", False):
        try:
            from exp_framework.fragmem_host import stop_fragmem
            stop_fragmem(serial)
        except Exception as error:
            print(f"[{serial}] stop_fragmem failed: {error}", file=sys.stderr)

    # vmstat end（失败只告警，不中断 odpm/lock_stat/derive 收尾）
    try:
        record_vmstat_end(serial, out_dir, keys=sess.vmstat_keys)
    except Exception as error:
        print(f"[{serial}] vmstat_end failed: {error}", file=sys.stderr)

    # odpm end + delta
    if sess.odpm_enabled:
        try:
            odpm_end_uptime = capture_odpm(serial, out_dir, "end")
            duration_s = max(odpm_end_uptime - sess.odpm_start_uptime, 0.001)
            d = odpm_delta(out_dir / "odpm_start.txt",
                           out_dir / "odpm_end.txt", duration_s)
            if d:
                (out_dir / "odpm_delta.json").write_text(
                    json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
        except Exception as error:
            print(f"[{serial}] odpm delta failed: {error}", file=sys.stderr)

    # lock_stat end + delta
    if sess.lock_stat_enabled and sess.lock_stat_start_text:
        try:
            end_text = capture_lock_stat(serial, out_dir / "lock_stat_end.txt")
            delta_text = lock_stat_delta(sess.lock_stat_start_text, end_text)
            (out_dir / "lock_stat_delta.txt").write_text(delta_text + "\n",
                                                         encoding="utf-8")
        except Exception as error:
            print(f"[{serial}] lock_stat delta failed: {error}", file=sys.stderr)

    # derive metrics
    scripts_dir = Path(__file__).resolve().parents[1]  # scripts/ 根（utils 同层）
    try:
        run_derive_metrics(scripts_dir=scripts_dir, out_dir=out_dir,
                           vmstat_start=out_dir / "vmstat_start.json",
                           vmstat_end=out_dir / "vmstat_end.json")
        vmstat_samples = out_dir / "vmstat_samples.csv"
        if vmstat_samples.exists():
            derive_vmstat_csv(vmstat_samples, out_dir / "vmstat_derived.csv")
    except Exception as error:
        print(f"[{serial}] derive metrics failed: {error}", file=sys.stderr)
