"""memstress 实验后端：app 冷启动压力循环。

私有逻辑：包解析/活动解析、设备端 cycle runner 生成与推送、完成轮询、
产物拉取与 cycle_timing/launch gate 解析。
采样/设备准备/信号清理全部由前端框架提供。
"""
import json
import random
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Sequence

from exp_framework.utils.adb_utils import adb_shell
from exp_framework.utils.signal_utils import (run_interruptible,
                                              sleep_interruptible)

from exp_framework.experiment.experiment import Experiment, register


# ---------------- 包/活动解析（memstress 私有） ----------------

def validate_packages(serial: str, pkgs: Sequence[str]) -> List[str]:
    """返回设备上已安装的包子集。"""
    out = adb_shell(serial, "pm list packages", timeout_s=30, check=False)
    installed = set()
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            installed.add(line.split(":", 1)[1])
    return [p for p in pkgs if p in installed]


def _extract_component_from_line(line: str, pkg: str) -> Optional[str]:
    for token in line.split():
        if "/" not in token:
            continue
        token = token.rstrip(":")
        if (token.startswith(pkg + "/") or token.startswith(pkg + ".")
                or token.startswith(pkg + "$")):
            return token
        if re.match(r"^[A-Za-z0-9_.]+/", token):
            return token
    return None


def _resolve_activity_from_dumpsys(output: str, pkg: str) -> Optional[str]:
    lines = output.splitlines()
    for index, raw_line in enumerate(lines):
        if "android.intent.action.MAIN:" not in raw_line:
            continue
        search_end = min(len(lines), index + 80)
        candidate: Optional[str] = None
        for next_line in lines[index + 1:search_end]:
            stripped = next_line.strip()
            if not stripped:
                continue
            if stripped.endswith(":") and not stripped.startswith("Category:"):
                break
            extracted = _extract_component_from_line(stripped, pkg)
            if extracted is not None:
                candidate = extracted
                continue
            if (candidate is not None
                    and 'Category: "android.intent.category.LAUNCHER"' in stripped):
                return candidate
    return None


def resolve_activity(serial: str, pkg: str) -> Optional[str]:
    """解析包的 LAUNCHER 组件（多级回退）。"""
    out = adb_shell(serial, f"pm resolve-activity --brief {pkg}",
                    timeout_s=10, check=False)
    for line in out.splitlines():
        line = line.strip()
        if "/" in line and not line.startswith("Error"):
            return line
    out2 = adb_shell(
        serial,
        f"cmd package resolve-activity --brief -a android.intent.action.MAIN "
        f"-c android.intent.category.LAUNCHER {pkg}",
        timeout_s=10, check=False)
    for line in out2.splitlines():
        line = line.strip()
        if "/" in line:
            return line
    out3 = adb_shell(serial, f"dumpsys package {pkg}", timeout_s=20,
                     check=False)
    resolved = _resolve_activity_from_dumpsys(out3, pkg)
    if resolved is not None:
        return resolved
    return None


def start_activity(serial: str, component: str,
                   float_extras: Optional[Dict[str, float]] = None) -> str:
    shell_cmd = "am start -W -n " + shlex.quote(component)
    for key, value in (float_extras or {}).items():
        shell_cmd += " --ef " + shlex.quote(key) + " " + shlex.quote(f"{float(value):.8g}")
    cp = subprocess.run(["adb", "-s", serial, "shell", shell_cmd],
                        capture_output=True, text=True, timeout=90)
    output = ((cp.stdout or "") + (cp.stderr or "")).strip()
    bad_markers = ["Error:", "Exception", "not found", "does not exist",
                   "result=", "Status: timeout"]
    if cp.returncode != 0 or any(marker in output for marker in bad_markers):
        raise RuntimeError(f"am start failed for {component}: rc={cp.returncode} "
                           f"output={output[:400]}")
    return output


def exit_to_home(serial: str):
    subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent",
                    "KEYCODE_HOME"], capture_output=True, timeout=10)


def force_stop_packages(serial: str, pkgs: Sequence[str]):
    for pkg in pkgs:
        subprocess.run(["adb", "-s", serial, "shell", "am", "force-stop", pkg],
                       capture_output=True, timeout=15)


def launch_and_background(serial: str, component: str, hold_ms: int,
                          mode: str,
                          float_extras: Optional[Dict[str, float]] = None):
    start_activity(serial, component, float_extras=float_extras)
    if mode == "interactive":
        time.sleep(0.6)
        from exp_framework.utils.interactive import interactive_click_loop
        interactive_click_loop(serial)
    time.sleep(max(0, hold_ms) / 1000.0)
    exit_to_home(serial)


# ---------------- 设备端 cycle runner 脚本生成 ----------------

def _generate_device_cycle_script(
    *,
    packages: List[str],
    components: List[str],
    max_cycles: int,
    burst_size: int,
    hold_ms: int,
    launch_gap_ms: int,
    cycle_sleep_ms: int,
    seed: int,
    mode: str,
    float_extras: Dict[str, float],
) -> str:
    """生成设备端 memstress cycle 脚本（Android sh）。"""
    extras_args = ""
    for k, v in float_extras.items():
        extras_args += " --ef " + shlex.quote(k) + " " + shlex.quote(f"{v:.8g}")

    # 与历史 host 端算法完全一致：持久 random.Random(seed)，每 cycle 洗牌
    # 副本后取 order[:burst_size]；由 host 生成具体顺序写入脚本。
    rng = random.Random(seed)
    cycle_cases: List[str] = []
    for cycle in range(1, max_cycles + 1):
        order = list(components)
        rng.shuffle(order)
        selected = order[:max(1, burst_size)]
        argv = " ".join(shlex.quote(comp) for comp in selected)
        cycle_cases.append(f"  {cycle}) set -- {argv} ;;")
    cycle_case_body = "\n".join(cycle_cases)

    script = f"""#!/system/bin/sh
# Auto-generated memstress cycle runner
echo -1000 > /proc/self/oom_score_adj

SELF=/data/local/tmp/runner_self.log
echo "start pid=$$ adj=$(cat /proc/self/oom_score_adj 2>/dev/null) ppid=$PPID up=$(awk '{{print $1}}' /proc/uptime)" >> "$SELF"
trap 'echo "trap EXIT up=$(cat /proc/uptime)" >> "$SELF"' EXIT
trap 'echo "trap TERM/INT up=$(cat /proc/uptime)" >> "$SELF"; echo "terminated_by_signal up=$(cat /proc/uptime)" > "$DONE"; exit 143' TERM INT

LOG=/data/local/tmp/memstress_cycles.tsv
EVENTS=/data/local/tmp/memstress_events.tsv
DONE=/data/local/tmp/memstress_done
STOP=/data/local/tmp/memstress_stop
rm -f "$LOG" "$EVENTS" "$DONE" "$STOP"

MAX_CYCLES={max_cycles}
BURST_SIZE={burst_size}
HOLD_MS={hold_ms}
LAUNCH_GAP_MS={launch_gap_ms}
CYCLE_SLEEP_MS={cycle_sleep_ms}
SEED={seed}
MODE={shlex.quote(mode)}

sanitize_one_line() {{
  printf '%s' "$1" | tr '\\015\\012\\011' '   ' | cut -c 1-400
}}

sleep_ms() {{
  ms=$1
  [ "$ms" -le 0 ] && return 0
  if command -v usleep >/dev/null 2>&1; then
    usleep $((ms * 1000))
    return $?
  fi
  if command -v toybox >/dev/null 2>&1; then
    toybox usleep $((ms * 1000))
    return $?
  fi
  echo "missing_usleep ms=$ms" >> "$EVENTS"
  return 97
}}

# sleep in 200ms slices, aborting as soon as STOP appears
sleep_ms_check() {{
  ms=$1
  [ "$ms" -le 0 ] && return 0
  step=200
  while [ "$ms" -gt 0 ]; do
    [ -e "$STOP" ] && {{ echo "stopped cycle=$cycle" > "$DONE"; exit 143; }}
    if [ "$ms" -ge "$step" ]; then
      sleep_ms "$step" || return $?
      ms=$((ms - step))
    else
      sleep_ms "$ms" || return $?
      ms=0
    fi
  done
}}

if [ "$MODE" != "launch_only" ]; then
  echo "unsupported_mode=$MODE" > "$DONE"
  exit 96
fi

total_ok=0
total_err=0

for cycle in $(seq 1 $MAX_CYCLES); do
  [ -e "$STOP" ] && {{ echo "stopped cycle=$cycle" > "$DONE"; exit 143; }}
  echo "hb cycle=$cycle up=$(awk '{{print $1}}' /proc/uptime)" >> "$SELF"
  cycle_start_ts=$(awk '{{print $1}}' /proc/uptime)
  human_ts=$(date '+%Y-%m-%d %H:%M:%S')

  case "$cycle" in
{cycle_case_body}
  *) set -- ;;
  esac

  ok=0
  err=0
  pos=0
  total_this_cycle=$#

  for comp in "$@"; do
    [ -e "$STOP" ] && {{ echo "stopped cycle=$cycle" > "$DONE"; exit 143; }}
    pos=$((pos + 1))
    ts_start=$(awk '{{print $1}}' /proc/uptime)
    output=$(timeout 10 am start -W -n "$comp"{extras_args} 2>&1)
    rc=$?
    if [ $rc -eq 124 ]; then
      # am start hung: take a targeted state snapshot (who blocks the launch),
      # nudge the UI once (mimics a finger touch), then move on immediately.
      ts_timeout=$(awk '{{print $1}}' /proc/uptime)
      comp_pkg=${{comp%/*}}
      echo "=== cycle=$cycle comp=$comp hang @${{ts_timeout}}s ===" >> /data/local/tmp/hang_snapshot.log
      {{ echo ---pidof---; pidof "$comp_pkg" 2>/dev/null; echo ---proc-stat---; ps -A -o PID,STAT,NAME 2>/dev/null | awk -v p="$comp_pkg" '$3 ~ p {{print}}'; echo ---resumed---; dumpsys activity activities 2>/dev/null | awk '/mResumedActivity|mFocusedApp/ {{print}}'; echo ---dropbox-anr---; ls -t /data/system/dropbox 2>/dev/null | head -5; echo ---dmesg---; dmesg 2>/dev/null | tail -10; }} >> /data/local/tmp/hang_snapshot.log 2>&1
      input tap 540 1200 >/dev/null 2>&1
      sleep 1
      ts_end=$(awk '{{print $1}}' /proc/uptime)
      elapsed=$(awk -v a="$ts_start" -v b="$ts_end" 'BEGIN {{printf "%.2f", b - a}}')
      printf '%s\ttimeout\t%s\t%s\t%s\t%s\t%s\n' "$cycle" "$comp" "$rc" "$elapsed" "$ts_start" "timeout10 nudged" >> "$EVENTS"
      continue
    fi
    ts_end=$(awk '{{print $1}}' /proc/uptime)
    elapsed=$(awk -v a="$ts_start" -v b="$ts_end" 'BEGIN {{printf "%.2f", b - a}}')
    bad=0
    is_timeout=0
    case "$output" in
      *"Error:"*|*"Exception"*|*"not found"*|*"does not exist"*|*"result="*) bad=1 ;;
      *"Status: timeout"*|*"ANR"*|*"not responding"*) is_timeout=1 ;;
    esac
    if [ $rc -eq 0 ] && [ $bad -eq 0 ] && [ $is_timeout -eq 0 ]; then
      ok=$((ok+1))
      printf '%s\tok\t%s\t%s\t%s\t%s\t\n' "$cycle" "$comp" "$rc" "$elapsed" "$ts_start" >> "$EVENTS"
      if ! sleep_ms_check "$HOLD_MS"; then
        echo "missing_sleep_helper hold_ms=$HOLD_MS" > "$DONE"
        exit 97
      fi
      input keyevent KEYCODE_HOME >/dev/null 2>&1
    elif [ $is_timeout -eq 1 ]; then
      msg=$(sanitize_one_line "$output")
      printf '%s\ttimeout\t%s\t%s\t%s\t%s\t%s\n' "$cycle" "$comp" "$rc" "$elapsed" "$ts_start" "$msg" >> "$EVENTS"
    else
      err=$((err+1))
      msg=$(sanitize_one_line "$output")
      printf '%s\terror\t%s\t%s\t%s\t%s\t%s\n' "$cycle" "$comp" "$rc" "$elapsed" "$ts_start" "$msg" >> "$EVENTS"
    fi
    if [ $pos -lt $total_this_cycle ]; then
      if ! sleep_ms_check "$LAUNCH_GAP_MS"; then
        echo "missing_sleep_helper launch_gap_ms=$LAUNCH_GAP_MS" > "$DONE"
        exit 97
      fi
    fi
  done

  printf '%s\t%s\t%s\t%s\t%s\n' "$cycle" "$cycle_start_ts" "$human_ts" "$ok" "$err" >> "$LOG"

  total_ok=$((total_ok + ok))
  total_err=$((total_err + err))

  if [ $err -gt 0 ] || [ $ok -eq 0 ]; then
    echo "failed_launch_gate cycle=$cycle ok=$ok err=$err" > "$DONE"
    exit 11
  fi

  if [ $cycle -lt $MAX_CYCLES ]; then
    if ! sleep_ms_check "$CYCLE_SLEEP_MS"; then
      echo "missing_sleep_helper cycle_sleep_ms=$CYCLE_SLEEP_MS" > "$DONE"
      exit 97
    fi
  fi
done

echo "total_cycles=$MAX_CYCLES total_ok=$total_ok total_err=$total_err" > "$DONE"
"""
    return script


# ---------------- memstress 后端 ----------------

@register("memstress")
class Memstress(Experiment):
    """app 冷启动压力实验：每 cycle 洗牌取 burst 个包启动。"""

    def _cfg(self, key: str, default=None):
        return self.backend_config.get(key, default)

    # ---- prepare：设备准备 + 包/活动解析 ----

    def prepare(self) -> Dict[str, Any]:
        from exp_framework.utils.device_prep import ensure_awake_unlocked_and_stay_awake
        ensure_awake_unlocked_and_stay_awake(
            self.serial, out_dir=self.out_dir, retries=3, retry_sleep_s=2,
            stop_event=self.stop_event)
        packages = list(self._cfg("packages", []) or [])
        if not packages:
            raise RuntimeError("backend memstress: no packages in config")
        valid = validate_packages(self.serial, packages)
        skipped = [p for p in packages if p not in valid]
        if skipped:
            print(f"[{self.serial}] skipped (not installed): {skipped}")
        resolved: Dict[str, str] = {}
        for pkg in valid:
            comp = resolve_activity(self.serial, pkg)
            if comp:
                resolved[pkg] = comp
            else:
                print(f"[{self.serial}] could not resolve activity for {pkg}")
        if not resolved:
            raise RuntimeError("no launchable activities found")

        (self.work_dir / "resolved_activities.json").write_text(
            json.dumps(resolved, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        try:
            start_meminfo = run_interruptible(
                self.stop_event,
                ["adb", "-s", self.serial, "shell", "dumpsys", "meminfo"],
                timeout_s=60)
            (self.work_dir / "dumpsys_meminfo_start.txt").write_text(
                start_meminfo.stdout or "", encoding="utf-8")
        except Exception:
            pass
        self._resolved = resolved

        # ---- 打碎 precondition（backend.config 配置 precondition_monkey 块时执行）----
        # 流程：killall 清后台 → 临时关 kfragd/force_reclaim（不整理碎片，4b verify 会设回）
        # → 网络开 → 刷抖音直到 order2+ < threshold → 断网（冷启动等效飞行模式）
        pm = self._cfg("precondition_monkey", {}) or {}
        if pm.get("enabled"):
            from exp_framework.precondition_monkey import (fragment_douyin_until,
                                                           set_network)
            from exp_framework.utils import device_nodes as _dn

            if pm.get("killall_before", True):
                adb_shell(self.serial, "am kill-all || true",
                          timeout_s=15, check=False)
                time.sleep(2)
            for path, val in (("/proc/sys/vm/kfragd_enabled", "0"),
                              ("/proc/sys/vm/kfragd_force_reclaim", "0")):
                _dn.set_node(self.serial, path, val)

            set_network(self.serial, enabled=True)
            r = fragment_douyin_until(
                self.serial,
                threshold=int(pm.get("buddy_threshold", 2000)),
                max_swipes=int(pm.get("max_swipes", 400)),
                gap_s=float(pm.get("gap_s", 0.1)))
            set_network(self.serial, enabled=False)
            print(f"[{self.serial}] fragment result: {r}", file=sys.stderr)
            self._fragment_result = r

        result: Dict[str, Any] = {"packages_resolved": resolved}
        if getattr(self, "_fragment_result", None):
            result["fragment"] = self._fragment_result
        return result

    # ---- run：生成/推送 runner -> 轮询完成 -> pull/解析 ----

    def run(self) -> Dict[str, Any]:
        resolved = getattr(self, "_resolved", {})
        components = list(resolved.values())
        max_cycles = int(self._cfg("max_cycles", 50))
        seed = int(self._cfg("seed", 20260617))
        hold_ms = int(self._cfg("hold_ms", 15))
        launch_gap_ms = int(self._cfg("launch_gap_ms", 15))
        cycle_sleep_ms = int(self._cfg("cycle_sleep_ms", 1000))
        burst_size = max(1, int(self._cfg("burst_size", 4)))
        mode = str(self._cfg("mode", "launch_only"))

        synthetic_float_extras: Dict[str, float] = {}
        for key, cfg_key in (
            ("zz_mthp_vma_count_scale", "synthetic_vma_count_scale"),
            ("zz_mthp_anon_vma_size_scale", "synthetic_anon_vma_size_scale"),
            ("zz_mthp_cow_pages_scale", "synthetic_cow_pages_scale"),
            ("zz_mthp_filemap_size_scale", "synthetic_filemap_size_scale"),
            ("zz_mthp_dlopen_lib_count_scale", "synthetic_dlopen_lib_count_scale"),
        ):
            val = float(self._cfg(cfg_key, 1.0))
            if val != 1.0:
                synthetic_float_extras[key] = val

        # 生成并推送设备端 runner
        device_script = _generate_device_cycle_script(
            packages=list(self._cfg("packages", [])),
            components=components,
            max_cycles=max_cycles, burst_size=burst_size,
            hold_ms=hold_ms, launch_gap_ms=launch_gap_ms,
            cycle_sleep_ms=cycle_sleep_ms, seed=seed, mode=mode,
            float_extras=synthetic_float_extras,
        )
        local_script = self.work_dir / "device_cycle_runner.sh"
        local_script.write_text(device_script, encoding="utf-8")

        if not self.stop_event.is_set():
            started = False
            for attempt in range(5):
                try:
                    push_cp = run_interruptible(
                        self.stop_event,
                        ["adb", "-s", self.serial, "push", str(local_script),
                         "/data/local/tmp/device_cycle_runner.sh"],
                        timeout_s=60)
                    if push_cp.returncode != 0:
                        raise RuntimeError((push_cp.stdout or "")
                                           + (push_cp.stderr or ""))
                    start_cmd = (
                        "chmod 755 /data/local/tmp/device_cycle_runner.sh && "
                        "rm -f /data/local/tmp/memstress_done "
                        "/data/local/tmp/memstress_stop "
                        "/data/local/tmp/memstress_cycles.tsv "
                        "/data/local/tmp/memstress_events.tsv && "
                        "(setsid /data/local/tmp/device_cycle_runner.sh "
                        "</dev/null >/dev/null 2>&1 &)")
                    from exp_framework.utils import adb_utils
                    adb_utils.adb_shell_root(self.serial, start_cmd,
                                             timeout_s=60, check=True)
                    started = True
                    break
                except Exception as e:
                    print(f"[{self.serial}] push/start attempt {attempt + 1} failed: {e}",
                          file=sys.stderr)
                    sleep_interruptible(self.stop_event, 10)
            if not started:
                raise RuntimeError(f"failed to push/start device cycle runner "
                                   f"on {self.serial}")
            print(f"[{self.serial}] device cycle runner started "
                  f"(max_cycles={max_cycles})", file=sys.stderr)

            # 轮询完成 / 进度 / heartbeat 超时；响应 stop_event
            poll_interval_s = 0.5
            last_lines = 0
            last_heartbeat_bucket = 0
            last_heartbeat_ts = time.monotonic()
            heartbeat_timeout_s = int(
                self._cfg("heartbeat_timeout_s", 0) or 0)
            # 设备断连兜底：连续 adb 失败 max 次 → 判定设备丢失，快速退出
            # （避免设备离线后无限空转的僵尸循环）
            adb_fail_streak = 0
            max_adb_fail = int(self._cfg("adb_fail_timeout", 5))
            while True:
                sleep_interruptible(self.stop_event, poll_interval_s)
                if self.stop_event.is_set():
                    subprocess.run(
                        ["adb", "-s", self.serial, "shell",
                         "touch /data/local/tmp/memstress_stop"],
                        capture_output=True, timeout=15)
                try:
                    done_out = run_interruptible(
                        self.stop_event,
                        ["adb", "-s", self.serial, "shell",
                         "cat /data/local/tmp/memstress_done 2>/dev/null"],
                        timeout_s=15).stdout.strip()
                except Exception:
                    done_out = ""
                if done_out:
                    print(f"[{self.serial}] device cycle runner completed: {done_out}",
                          file=sys.stderr)
                    break
                try:
                    lines_out = run_interruptible(
                        self.stop_event,
                        ["adb", "-s", self.serial, "shell",
                         "wc -l < /data/local/tmp/memstress_cycles.tsv 2>/dev/null"],
                        timeout_s=15).stdout.strip()
                    cur_lines = int(lines_out) if lines_out.isdigit() else 0
                except Exception:
                    cur_lines = last_lines
                if cur_lines > last_lines:
                    last_lines = cur_lines
                    bucket = cur_lines // 10
                    if bucket > last_heartbeat_bucket:
                        last_heartbeat_bucket = bucket
                        last_heartbeat_ts = time.monotonic()
                        print(f"[{self.serial}] device cycles: {cur_lines}/{max_cycles}",
                              file=sys.stderr)
                # 设备连通性探测（区分"无新进展"与"adb 真失败"）：
                # 仅统计显式探测失败，避免 cycle 慢被误判为设备丢失
                if not self.stop_event.is_set():
                    try:
                        alive = run_interruptible(
                            self.stop_event,
                            ["adb", "-s", self.serial, "shell", "echo ok"],
                            timeout_s=5).stdout.strip() == "ok"
                    except Exception:
                        alive = False
                    if alive:
                        adb_fail_streak = 0
                    else:
                        adb_fail_streak += 1
                        if adb_fail_streak >= max_adb_fail:
                            msg = (f"[{self.serial}] device lost: "
                                   f"{adb_fail_streak} consecutive adb failures "
                                   f"(device offline?)")
                            print(msg, file=sys.stderr)
                            raise RuntimeError(msg)
                if (heartbeat_timeout_s
                        and time.monotonic() - last_heartbeat_ts
                        >= heartbeat_timeout_s):
                    msg = (f"[{self.serial}] heartbeat timeout: no 10-cycle "
                           f"progress for {heartbeat_timeout_s}s "
                           f"(last_cycle={last_lines}/{max_cycles})")
                    print(msg, file=sys.stderr)
                    try:
                        subprocess.run(
                            ["adb", "-s", self.serial, "shell",
                             "touch /data/local/tmp/memstress_stop"],
                            capture_output=True, timeout=15)
                    except Exception:
                        pass
                    raise RuntimeError(msg)
        else:
            print(f"[{self.serial}] stop requested before runner start; "
                  f"skipping workload", file=sys.stderr)

        # pull 设备端产物
        for remote, local in (
            ("/data/local/tmp/memstress_cycles.tsv", "device_cycles.tsv"),
            ("/data/local/tmp/memstress_events.tsv", "device_events.tsv"),
            ("/data/local/tmp/memstress_done", "device_done.txt"),
            ("/data/local/tmp/hang_snapshot.log", "hang_snapshot.log"),
            ("/data/local/tmp/runner_self.log", "runner_self.log"),
        ):
            try:
                run_interruptible(
                    self.stop_event,
                    ["adb", "-s", self.serial, "pull", remote,
                     str(self.work_dir / local)],
                    timeout_s=30)
            except Exception:
                pass

        # 解析 cycle_log.jsonl / cycle_timing / launch gate
        launch_failures: List[str] = []
        cycle_start_ts: List[float] = []
        events_by_cycle: Dict[int, Dict[str, List[str]]] = {}
        timeout_events: List[Dict] = []
        launch_elapsed: List[float] = []
        ordered_ts: List[float] = []

        events_path = self.work_dir / "device_events.tsv"
        if events_path.exists():
            for line in events_path.read_text(
                    encoding="utf-8", errors="ignore").splitlines():
                fields = line.split("\t", 6)
                if len(fields) < 4:
                    continue
                try:
                    cycle_num = int(fields[0])
                except ValueError:
                    continue
                kind, comp, rc = fields[1], fields[2], fields[3]
                elapsed = 0.0
                mono_ts = 0.0
                try:
                    elapsed = float(fields[4]) if fields[4] else 0.0
                    mono_ts = float(fields[5]) if fields[5] else 0.0
                except ValueError:
                    pass
                output = fields[6] if len(fields) > 6 else ""
                if mono_ts > 0:
                    ordered_ts.append(mono_ts)
                entry = events_by_cycle.setdefault(
                    cycle_num, {"launched": [], "errors": []})
                if kind == "ok":
                    entry["launched"].append(comp)
                    launch_elapsed.append(elapsed)
                elif kind == "timeout":
                    timeout_events.append({"cycle": cycle_num, "comp": comp,
                                           "elapsed": elapsed, "mono_ts": mono_ts})
                elif kind == "error":
                    entry["errors"].append(
                        f"{comp}:am start failed for {comp}: rc={rc} "
                        f"output={output[:400]}")
                else:
                    entry["errors"].append(line)

        cycle_log_f = (self.work_dir / "cycle_log.jsonl").open(
            "w", encoding="utf-8")
        device_log_path = self.work_dir / "device_cycles.tsv"
        if device_log_path.exists():
            for line in device_log_path.read_text(
                    encoding="utf-8", errors="ignore").splitlines():
                fields = line.split("\t")
                if len(fields) < 5:
                    continue
                try:
                    cycle_num = int(fields[0])
                    ts_epoch = float(fields[1])
                    ok_count = int(fields[3])
                    err_count = int(fields[4])
                except ValueError:
                    continue
                ts_text = fields[2]
                cycle_start_ts.append(ts_epoch)
                event = events_by_cycle.get(cycle_num, {"launched": [], "errors": []})
                errors_list = list(event["errors"])
                if err_count > 0 and not errors_list:
                    errors_list = [f"cycle {cycle_num}: {err_count} errors"]
                cycle_row = {"cycle": cycle_num,
                             "launched": event["launched"],
                             "errors": errors_list, "ts": ts_text}
                cycle_log_f.write(json.dumps(cycle_row, ensure_ascii=False) + "\n")
                cycle_log_f.flush()
                if (errors_list or ok_count == 0
                        or not event["launched"]):
                    launch_failures.extend(errors_list
                                           or ["no components launched"])
                    print(f"[{self.serial}] launch gate failed at cycle "
                          f"{cycle_num}: launched={len(event['launched'])} "
                          f"errors={len(errors_list)}", file=sys.stderr)
                    break
        cycle_log_f.close()

        done_path = self.work_dir / "device_done.txt"
        if done_path.exists():
            done_text = done_path.read_text(
                encoding="utf-8", errors="ignore").strip()
            if (done_text and not done_text.startswith("total_cycles=")
                    and not launch_failures):
                launch_failures.append(done_text)

        timing: Dict[str, Any] = {}
        if len(cycle_start_ts) >= 2:
            deltas = [cycle_start_ts[i + 1] - cycle_start_ts[i]
                      for i in range(len(cycle_start_ts) - 1)]
            deltas_sorted = sorted(deltas)
            n = len(deltas_sorted)
            total_s = cycle_start_ts[-1] - cycle_start_ts[0]
            import bisect
            deduct_s = 0.0
            for t in timeout_events:
                i = bisect.bisect_left(ordered_ts, t["mono_ts"])
                if i + 1 < len(ordered_ts):
                    deduct_s += ordered_ts[i + 1] - ordered_ts[i]
                else:
                    deduct_s += t["elapsed"] + 4.0
            launch = sorted(launch_elapsed)
            launch_stats = {}
            if launch:
                launch_stats = {
                    "launch_count": len(launch),
                    "launch_p50_s": round(launch[len(launch) // 2], 3),
                    "launch_p90_s": round(launch[int(len(launch) * 0.90)], 3),
                    "launch_max_s": round(launch[-1], 3),
                }
            timing = {
                "total_cycles": len(cycle_start_ts),
                "total_elapsed_s": round(total_s, 3),
                "total_elapsed_ms": round(total_s * 1000, 1),
                "timeout_count": len(timeout_events),
                "timeout_deduct_s": round(deduct_s, 3),
                "total_effective_s": round(max(total_s - deduct_s, 0.0), 3),
                "max_cycle_s": round(max(deltas), 3),
                "min_cycle_s": round(min(deltas), 3),
                "mean_cycle_s": round(sum(deltas) / n, 3),
                "median_cycle_s": round(deltas_sorted[n // 2], 3),
                "p90_cycle_s": round(deltas_sorted[int(n * 0.90)], 3),
                "p95_cycle_s": round(deltas_sorted[int(n * 0.95)], 3),
                "deltas_s": [round(x, 3) for x in deltas],
                "unit": "seconds",
            }
            timing.update(launch_stats)
            (self.work_dir / "cycle_timing.json").write_text(
                json.dumps(timing, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            (self.work_dir / "cycle_timing.md").write_text(
                "\n".join([
                    "# cycle timing (per-cycle wall-clock)\n",
                    f"- cycles: {timing['total_cycles']}",
                    f"- total: {timing['total_elapsed_s']} s "
                    f"({timing['total_elapsed_ms']} ms)",
                    f"- hangs: {timing['timeout_count']} "
                    f"(deducted {timing['timeout_deduct_s']} s, effective "
                    f"{timing['total_effective_s']} s)",
                    f"- per-app launch: {timing.get('launch_count', 0)} ok, "
                    f"p50={timing.get('launch_p50_s', '-')} s "
                    f"p90={timing.get('launch_p90_s', '-')} s "
                    f"max={timing.get('launch_max_s', '-')} s",
                    f"- mean: {timing['mean_cycle_s']} s",
                    f"- max: {timing['max_cycle_s']} s",
                    f"- p90: {timing['p90_cycle_s']} s",
                    f"- p95: {timing['p95_cycle_s']} s",
                ]) + "\n", encoding="utf-8")

        if launch_failures:
            (self.work_dir / "launch_failures.txt").write_text(
                "\n".join(launch_failures) + "\n", encoding="utf-8")
            return {"launch_failures": launch_failures[:20],
                    "timing": timing, "total_cycles": len(cycle_start_ts)}
        return {"launch_failures": [], "timing": timing,
                "total_cycles": len(cycle_start_ts)}

    def stop_device(self) -> None:
        """立即请求设备端 cycle runner 退出（幂等；信号/异常清理时调用）。"""
        try:
            from exp_framework.utils import adb_utils
            adb_utils.adb_shell_root(self.serial, "touch /data/local/tmp/memstress_stop",
                                     timeout_s=15, check=False)
        except Exception:
            pass

    def cleanup(self) -> None:
        self.stop_device()
        # 解除 global_freq_lock（prepare 阶段锁的 80%），恢复设备自由调频
        try:
            from exp_framework.utils.device_prep import LOCK_FREQ_UNLOCK_CMD
            from exp_framework.utils import adb_utils
            adb_utils.adb_shell_root(self.serial, LOCK_FREQ_UNLOCK_CMD,
                                     timeout_s=10, check=False)
        except Exception:
            pass
