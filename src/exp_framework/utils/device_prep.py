from __future__ import annotations

import time
from exp_framework.utils.signal_utils import sleep_interruptible
from datetime import datetime
from pathlib import Path
from typing import Tuple

from . import adb_utils
from .adb_utils import adb_shell, adb_shell_retry


def _read_thermal_zone(serial: str, zone: str) -> float:
    from exp_framework.utils import adb_utils
    out = adb_utils.adb_shell_root(
        serial, f"cat /sys/class/thermal/{zone}/temp 2>/dev/null || echo -1",
        timeout_s=10, check=False)
    val = out.strip()
    if not val:
        raise RuntimeError(f"adb returned empty output for {zone} temp on {serial}")
    try:
        celsius = float(val) / 1000.0
    except ValueError:
        raise RuntimeError(f"unexpected temp value {val!r} for {zone} on {serial}")
    if celsius < 0:
        raise RuntimeError(f"reading {zone} temp failed on device {serial} (raw={val!r})")
    return celsius


COOLDOWN_ZONES = {
    "thermal_zone0": 50.0,   # BIG
    "thermal_zone1": 55.0,   # MID
    "thermal_zone2": 60.0,   # LITTLE (small cores, low impact)
    "thermal_zone3": 55.0,   # G3D (GPU)
    "thermal_zone25": 40.0,  # battery
}
# 皮肤温度安全阈值（zone16=quiet_therm，VIRTUAL-SKIN 55°C 关机红线，留 10°C 余量）。
# CPU 判据会"假达标"（CPU 降得快、机身热容降得慢——连续实验轮间机身蓄热
# 累积，R4 冷却中皮肤过 55°C 热关机）；皮肤 < 45°C 是达标必要条件。
SKIN_SAFE_C = 45.0
PLATEAU_DELTA_C = 1.0        # plateau: delta T <= 1C between samples
PLATEAU_SAMPLES = 3          # consecutive samples for plateau/abs-stable
PLATEAU_MAX_C = 65.0         # plateau balance temperature upper bound
COOLDOWN_TIMEOUT_S = 600
FRAMEWORK_READY_TIMEOUT_S = 180
# framework start 后稳定等待：get-config 可达 ≠ 系统完全就绪，
# 冷启动多 app（burst）需要 zygote/AMS/PMS 完全稳定，否则偶发启动失败
# （实测 start 后立刻 am start 报 Can't find service: activity）。
FRAMEWORK_SETTLE_S = 15

# 锁频 ~75% of max（全部 3 个 cluster；就近可用 OPP：73.7% / 73.8% / 73.1%）。
# 80% 档（1401/1836/2252）实测 40 轮 BIG 贴 115°C 内核 trip 线，降至 75% 留余量。
# 通过内核 global_freq_lock 锁定（屏蔽一切其他频率设置源）：
# 写 cpuN/cpufreq/global_freq_lock = 锁定值（kHz），写 0 解除。
GFL = "/sys/devices/system/cpu/cpu$i/cpufreq/global_freq_lock"

LOCK_FREQ_75PCT_CMD = (
    "for i in 0 1 2 3; do "
    f"echo 1328000 > {GFL} 2>/dev/null; "
    "done; "
    "for i in 4 5; do "
    f"echo 1663000 > {GFL} 2>/dev/null; "
    "done; "
    "for i in 6 7; do "
    f"echo 2048000 > {GFL} 2>/dev/null; "
    "done")

# 冷却最低频：framework-stop 后锁到各核 cpuinfo_min（每 cluster 不同：
# little=300000 / mid=400000 / big=500000），空转降温最快。
# 不能写死单一值——store 校验 lock < cpuinfo_min 会 EINVAL。
LOCK_FREQ_MIN_CMD = (
    "for i in 0 1 2 3 4 5 6 7; do "
    f"m=$(cat /sys/devices/system/cpu/cpu$i/cpufreq/cpuinfo_min_freq 2>/dev/null); "
    f"[ -n \"$m\" ] && echo $m > {GFL} 2>/dev/null; "
    "done")

# 解除频率锁（实验结束/解锁场景）
LOCK_FREQ_UNLOCK_CMD = (
    "for i in 0 1 2 3 4 5 6 7; do "
    f"echo 0 > {GFL} 2>/dev/null; "
    "done")


def ensure_zram(serial: str) -> None:
    """确保 zram swap 启用（框架级通用逻辑，sysctl_nodes 含 zram 节点时调用）。

    重启后 swap off 会导致 MADV_PAGEOUT/swap 相关实验无空间、orig_delta=0；
    disksize 由 sysctl_nodes 的 zram_disksize 节点设置（verify），本函数只做
    检查 + mkswap + swapon。
    """
    swaps = adb_utils.adb_shell_root(
        serial, "grep zram0 /proc/swaps", timeout_s=15, check=False)
    if "zram0" in swaps:
        return
    for c in ("mkswap /dev/block/zram0",
              "swapon -p 100 /dev/block/zram0"):
        adb_utils.adb_shell_root(serial, c, timeout_s=30, check=False)
    got = adb_utils.adb_shell_root(
        serial, "grep zram0 /proc/swaps", timeout_s=15, check=False)
    if "zram0" not in got:
        raise RuntimeError("zram swap 启用失败")


def cleanup_after_boot(serial: str, wait_after_boot_s: int = 90,
                       stop_event=None, fresh_boot_uptime_s: int = 300) -> dict:
    """Best-effort boot cleanup before a workload:
    - wait until the system is fully booted (boot_completed=1)
    - on a fresh boot, wait extra seconds for Android's background loading
      (BOOT_COMPLETED receivers, recents restore, preloads) to finish
    - force-stop ALL running app packages, am kill-all, drop caches
    Returns a small status dict for logging."""
    import subprocess
    status = {"booted": False, "settled": False,
              "force_stopped": False, "killed": False, "dropped": False}

    # 1) wait for boot_completed
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return status
        try:
            out = subprocess.run(["adb", "-s", serial, "shell",
                                  "getprop sys.boot_completed"],
                                 capture_output=True, text=True,
                                 timeout=15).stdout.strip()
        except Exception:
            out = ""
        if out == "1":
            status["booted"] = True
            break
        sleep_interruptible(stop_event, 5)
    if not status["booted"]:
        return status

    # 2) fresh boot: let Android's background loading settle
    try:
        out = subprocess.run(["adb", "-s", serial, "shell",
                              "cat /proc/uptime"],
                             capture_output=True, text=True,
                             timeout=15).stdout.strip()
        uptime = float(out.split()[0]) if out else 9999
    except Exception:
        uptime = 9999
    if uptime < fresh_boot_uptime_s:
        if stop_event is not None and stop_event.is_set():
            return status
        sleep_interruptible(stop_event, wait_after_boot_s)
        status["settled"] = True

    # 3) force-stop running THIRD-PARTY app packages (best effort; system
    #    apps are persistent and restart anyway). timeout wraps each
    #    force-stop so a stuck app cannot hang the loop.
    force_cmd = ("for p in $(pm list packages -3 2>/dev/null | cut -d: -f2); do "
                 "timeout 10 am force-stop $p 2>/dev/null; done")
    try:
        from exp_framework.utils import adb_utils
        adb_utils.adb_shell_root(serial, force_cmd, timeout_s=180, check=False)
        status["force_stopped"] = True
    except Exception:
        pass

    # 4) am kill-all (safety net)
    try:
        from exp_framework.utils import adb_utils
        adb_utils.adb_shell_root(serial, "am kill-all", timeout_s=30, check=False)
        status["killed"] = True
    except Exception:
        pass

    # 5) drop caches (page cache + dentries + inodes)
    try:
        from exp_framework.utils import adb_utils
        adb_utils.adb_shell_root(serial, "echo 3 > /proc/sys/vm/drop_caches",
                                 timeout_s=30, check=False)
        status["dropped"] = True
    except Exception:
        pass
    return status


def wait_for_cool_down(
    serial: str,
    zones: list = None,
    max_temps: dict = None,
    poll_s: int = 10,
    max_wait_s: int = COOLDOWN_TIMEOUT_S,
    plateau_samples: int = PLATEAU_SAMPLES,
    stop_event=None,
) -> dict:
    if zones is None:
        zones = list(COOLDOWN_ZONES.keys())
    if max_temps is None:
        max_temps = dict(COOLDOWN_ZONES)
    # 皮肤温度（zone16=quiet_therm，VIRTUAL-SKIN 55°C 关机红线代理）：
    # ① 日志必须带它（机身蓄热传导会让皮肤在冷却期间继续升，
    #    R4 冷却中热关机教训：CPU 已锁 min 但皮肤过 55°C）
    # ② 达标必要条件：皮肤 < SKIN_SAFE_C（CPU 判据会假达标——CPU 降得快
    #    机身热容降得慢，连续实验轮间蓄热累积）
    if "thermal_zone16" not in zones:
        zones = zones + ["thermal_zone16"]
    if "thermal_zone16" not in max_temps:
        max_temps["thermal_zone16"] = SKIN_SAFE_C
    t0 = time.time()
    stable_abs = 0
    prev_temps = None
    plateau_run = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            print("[cool_down] aborted by stop signal", flush=True)
            return {z: -1.0 for z in zones}
        temps = {}
        for z in zones:
            temps[z] = _read_thermal_zone(serial, z)
        elapsed = time.time() - t0
        parts = "  ".join(f"{z.split('_')[-1]}={temps[z]:.1f}°C" for z in zones)

        # Condition 1: absolute thresholds (all zones <= limit)
        abs_ok = all(temps[z] <= max_temps.get(z, 999) for z in zones) if all(t >= 0 for t in temps.values()) else False
        stable_abs = stable_abs + 1 if abs_ok else 0

        # Condition 2: plateau (delta T <= 1C for N samples) with balance <= 65C
        plateau_ok = False
        if prev_temps is not None:
            deltas = [abs(temps[z] - prev_temps[z]) for z in zones]
            if all(d <= PLATEAU_DELTA_C for d in deltas) and \
               all(t <= PLATEAU_MAX_C for t in temps.values()):
                plateau_run += 1
            else:
                plateau_run = 0
            if plateau_run >= plateau_samples:
                plateau_ok = True
        prev_temps = temps

        print(f"[cool_down] {parts}  abs={stable_abs}/{plateau_samples} "
              f"plateau={plateau_run}/{plateau_samples}  elapsed={elapsed:.0f}s", flush=True)
        if any(t < 0 for t in temps.values()):
            return {z: -1.0 for z in zones}
        if stable_abs >= plateau_samples or plateau_ok:
            return temps
        if elapsed >= max_wait_s:
            print(f"[cool_down] timeout after {elapsed:.0f}s", flush=True)
            return temps
        sleep_interruptible(stop_event, poll_s)


def cool_down_with_framework_stop(
    serial: str,
    *,
    stop_event=None,
    max_wait_s: int = COOLDOWN_TIMEOUT_S,
    log_path: Optional[Path] = None,
) -> dict:
    """自包含加速冷却（memstress 专用路径）：

    adb shell stop（zygote/system_server 全停，CPU 彻底空转）
      → wait_for_cool_down（最低频空转，降温最快）
      → 锁频 80%（冷却后锁，实验起始温度真实）
      → adb shell start（拉起 framework）
      → 等待就绪（cmd activity get-config 可达，180s 超时失败 raise）。
    """
    def _log(msg: str) -> None:
        print(f"[cool_down] {msg}", flush=True)
        if log_path is not None:
            try:
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(f"{msg}  {datetime.now().isoformat()}\n")
            except Exception:
                pass

    # 1) 停 framework：CPU 无任何用户态负载，空转降温最快
    _log("[framework_stop] adb shell stop")
    adb_utils.adb_shell_root(serial, "stop", timeout_s=15, check=False)

    # 2) 锁最低频（global_freq_lock = 300000kHz）：framework 已停 + CPU 锁最低频，
    #    空转降温最快（内核锁屏蔽一切其他频率设置源）
    _log("[lock_cpu_freq_min] global_freq_lock=300000")
    adb_utils.adb_shell_root(serial, LOCK_FREQ_MIN_CMD, timeout_s=10,
                             tty=True, check=False)

    # 3) 冷却（最低频空转）
    temps = wait_for_cool_down(serial, stop_event=stop_event,
                               max_wait_s=max_wait_s)

    # 4) 锁频 75%（冷却后锁，保证实验起始温度 = 锁频态真实温度）
    _log("[lock_cpu_freq_75pct] global_freq_lock=1328/1663/2048MHz")
    adb_utils.adb_shell_root(serial, LOCK_FREQ_75PCT_CMD, timeout_s=10,
                             tty=True, check=False)

    # 5) 拉起 framework 并等待就绪（就绪判定写死：activity 服务可达）
    _log("[framework_start] adb shell start")
    adb_utils.adb_shell_root(serial, "start", timeout_s=15, check=False)
    deadline = time.monotonic() + FRAMEWORK_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("aborted while waiting for framework ready")
        try:
            out = adb_utils.adb_shell_root(
                serial, "cmd activity get-config >/dev/null 2>&1 && echo ok",
                timeout_s=15, check=False)
            if "ok" in (out or ""):
                _log(f"[framework_start] ready, settling {FRAMEWORK_SETTLE_S}s "
                     f"(framework 刚起，冷启动多 app 需系统完全就绪)")
                sleep_interruptible(stop_event, FRAMEWORK_SETTLE_S)
                return temps
        except Exception:
            pass
        sleep_interruptible(stop_event, 5)
    raise RuntimeError("framework did not become ready after adb shell start "
                       f"({FRAMEWORK_READY_TIMEOUT_S}s)")


def is_device_awake(serial: str) -> Tuple[bool, str]:
    try:
        out = adb_shell(serial, "dumpsys power", timeout_s=30, check=True)
    except Exception as e:
        return False, f"ERR:{e}"

    wake_lines = [ln.strip() for ln in out.splitlines() if "mWakefulness" in ln]
    awake = any(("Awake" in ln) or ("mWakefulness=1" in ln) for ln in wake_lines)

    if wake_lines:
        summary = " | ".join(wake_lines[:4])
    else:
        summary = " | ".join(out.splitlines()[:3]).strip()
    return awake, summary


def ensure_awake_unlocked_and_stay_awake(
    serial: str,
    out_dir: Path,
    *,
    retries: int,
    retry_sleep_s: int,
    stop_event=None,
) -> None:
    """Best-effort device prep for stable long-running workloads.

    - wake screen
    - attempt to dismiss keyguard
    - set 'stay on' while plugged in
    - increase screen timeout
    - set SELinux permissive (setenforce 0) so root sysfs writes succeed
    - lock CPU frequencies to max for stable measurements
    """

    log_path = out_dir / "device_prepare_log.txt"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmds = [
        "input keyevent KEYCODE_WAKEUP || true",
        "wm dismiss-keyguard || true",
        "input keyevent KEYCODE_MENU || true",
        "input swipe 300 1400 300 400 200 || true",
        "svc power stayon true || true",
        "settings put global stay_on_while_plugged_in 3 || true",
        "settings put system screen_off_timeout 1800000 || true",
        # Airplane mode: Pixel tends to self-enable BT/WiFi after reboot;
        # keeps radio/wifi/bt scanning off during the experiment.
        "settings put global airplane_mode_on 1 || true",
        "am broadcast -a android.intent.action.AIRPLANE_MODE --ez state true || true",
        "svc wifi disable || true",
        "svc bluetooth disable || true",
        # Magisk posts a notification on every su grant; silent it (policy
        # stays allow, logging stays on -- only the toast/notification goes).
        "magisk --sqlite \"UPDATE policies SET notification=0\" 2>/dev/null || true",
        # Android re-loads background apps after reboot (BOOT_COMPLETED
        # receivers, recents restore, preloads); kill them so the workload
        # starts from a clean memory/CPU baseline.
        "am kill-all || true",
    ]

    with log_path.open("a", encoding="utf-8") as f:
        # Boot cleanup: force-stop all apps + drop caches (best effort)
        cleanup_status = cleanup_after_boot(serial, stop_event=stop_event)
        f.write(f"[boot_cleanup] {cleanup_status}  {datetime.now().isoformat()}\n")
        f.flush()
        if stop_event is not None and stop_event.is_set():
            f.write("[boot_cleanup] aborted by stop signal\n")
            f.flush()
            return

        for prep_cmd, label in (
            ("setenforce 0 2>/dev/null || true", "setenforce 0"),
            # Disable thermal passive downclocking (kernel cpufreq cooling
            # trips at 80C passive trip on the CPU zones; raise the trip so
            # the locked frequency is never overridden). Hot shutdown trip
            # stays; mitigated by the 80% lock (lower heat).
            ("echo 115000 > /sys/class/thermal/thermal_zone0/trip_point_2_temp 2>/dev/null || true; "
             "echo 115000 > /sys/class/thermal/thermal_zone1/trip_point_2_temp 2>/dev/null || true; "
             "echo 115000 > /sys/class/thermal/thermal_zone2/trip_point_2_temp 2>/dev/null || true",
             "disable_thermal_downclock"),
            # Skip boot dexopt so dex2oat does not compete with the workload
            # (already-compiled apps keep their odex; only uncompiled boot
            # apps interpret).
            ("setprop pm.dexopt.boot skip 2>/dev/null || true", "dexopt_boot_skip"),
            # 后台 dexopt 也关掉：JobScheduler 空闲/充电时触发的编译同样
            # 会污染冷启动耗时测量（bg-dexopt 与 boot 属性独立）。
            ("setprop pm.dexopt.bg-dexopt skip 2>/dev/null || true; "
             "setprop pm.dexopt.first-boot skip 2>/dev/null || true",
             "dexopt_bg_skip"),
        ):
            f.write(f"\n[{label}] {datetime.now().isoformat()}\n")
            f.write(f"$ {prep_cmd}\n")
            try:
                out = adb_utils.adb_shell_root(
                    serial, prep_cmd, timeout_s=10, tty=True, check=False)
                if out.strip():
                    f.write(out)
                    if not out.endswith("\n"):
                        f.write("\n")
            except Exception as e:
                f.write(f"ERR: {e}\n")

        for attempt in range(1, max(1, retries) + 1):
            f.write(f"\n[{attempt}] {datetime.now().isoformat()}\n")
            for cmd in cmds:
                f.write(f"$ {cmd}\n")
                try:
                    out = adb_shell_retry(
                        serial, cmd, timeout_s=20, retries=1, retry_sleep_s=1)
                    if out.strip():
                        f.write(out)
                        if not out.endswith("\n"):
                            f.write("\n")
                except Exception as e:
                    f.write(f"ERR: {e}\n")

            awake, wake_out = is_device_awake(serial)
            if wake_out:
                f.write(f"wake_out={wake_out}\n")
            f.write(f"awake={awake}\n")
            f.flush()
            if awake:
                break
            if attempt < max(1, retries):
                sleep_interruptible(stop_event, max(0, retry_sleep_s))
        else:
            f.write("[prep] never became awake within retries\n")
            f.flush()
            return

        # Cool down: 自包含加速冷却（stop framework → 空转冷却 → 锁频 80% →
        # start framework → 就绪确认）。锁频在冷却后执行，实验起始温度真实；
        # 冷却期间 CPU 最低频空转，降温比锁频态快得多。
        f.write(f"[cool_down] start {datetime.now().isoformat()}\n")
        temps: dict = {}
        try:
            temps = cool_down_with_framework_stop(
                serial, stop_event=stop_event, log_path=log_path)
        finally:
            f.write(f"[cool_down] done BIG={temps.get('thermal_zone0', -1):.1f}°C "
                    f"LITTLE={temps.get('thermal_zone2', -1):.1f}°C "
                    f"SKIN={temps.get('thermal_zone16', -1):.1f}°C "
                    f"(55°C 关机红线)  "
                    f"{datetime.now().isoformat()}\n")
            f.flush()

        # Framework start 后重新清理（cleanup_after_boot 在 framework-stop 前
        # 执行的效果被 framework 重启冲掉：start 后 system_server 重启、
        # 缓存重新填充——负载起点内存状态应是清理后的干净态，而非重启后的
        # 随机态。best effort：force-stop 三方包 + am kill-all + drop caches。
        f.write(f"[post_framework_cleanup] {datetime.now().isoformat()}\n")
        for post_cmd in (
            "for p in $(pm list packages -3 2>/dev/null | cut -d: -f2); do "
            "timeout 10 am force-stop $p 2>/dev/null; done",
            "am kill-all || true",
            "echo 3 > /proc/sys/vm/drop_caches 2>/dev/null; true",
        ):
            f.write(f"$ {post_cmd}\n")
            try:
                out = adb_utils.adb_shell_root(
                    serial, post_cmd, timeout_s=30, check=False)
                if out.strip():
                    f.write(out)
                    if not out.endswith("\n"):
                        f.write("\n")
            except Exception as e:
                f.write(f"ERR: {e}\n")
        f.write(f"[post_framework_cleanup] done {datetime.now().isoformat()}\n")
        f.flush()

