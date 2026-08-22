from __future__ import annotations

import time
from exp_framework.utils.signal_utils import sleep_interruptible
from datetime import datetime
from pathlib import Path
from typing import Tuple

from .adb_utils import adb_shell, adb_shell_retry


def _read_thermal_zone(serial: str, zone: str) -> float:
    import utils.adb_utils as adb_utils
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
PLATEAU_DELTA_C = 1.0        # plateau: delta T <= 1C between samples
PLATEAU_SAMPLES = 3          # consecutive samples for plateau/abs-stable
PLATEAU_MAX_C = 65.0         # plateau balance temperature upper bound
COOLDOWN_TIMEOUT_S = 600


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
        import utils.adb_utils as adb_utils
        adb_utils.adb_shell_root(serial, force_cmd, timeout_s=180, check=False)
        status["force_stopped"] = True
    except Exception:
        pass

    # 4) am kill-all (safety net)
    try:
        import utils.adb_utils as adb_utils
        adb_utils.adb_shell_root(serial, "am kill-all", timeout_s=30, check=False)
        status["killed"] = True
    except Exception:
        pass

    # 5) drop caches (page cache + dentries + inodes)
    try:
        import utils.adb_utils as adb_utils
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
            # Lock CPU frequencies to ~80% of max (all 3 clusters; nearest
            # available OPP per cluster: 77.7% / 81.5% / 80.4%) so the
            # workload runs at a fixed frequency without tripping thermal
            # (which would force downclock and break the locked-freq premise).
            ("for i in 0 1 2 3; do "
             "echo 1401000 > /sys/devices/system/cpu/cpu$i/cpufreq/scaling_min_freq 2>/dev/null; "
             "echo 1401000 > /sys/devices/system/cpu/cpu$i/cpufreq/scaling_max_freq 2>/dev/null; "
             "done; "
             "for i in 4 5; do "
             "echo 1836000 > /sys/devices/system/cpu/cpu$i/cpufreq/scaling_min_freq 2>/dev/null; "
             "echo 1836000 > /sys/devices/system/cpu/cpu$i/cpufreq/scaling_max_freq 2>/dev/null; "
             "done; "
             "for i in 6 7; do "
             "echo 2252000 > /sys/devices/system/cpu/cpu$i/cpufreq/scaling_min_freq 2>/dev/null; "
             "echo 2252000 > /sys/devices/system/cpu/cpu$i/cpufreq/scaling_max_freq 2>/dev/null; "
             "done", "lock_cpu_freq_80pct"),
            # Disable thermal passive downclocking (kernel cpufreq cooling
            # trips at 80C passive trip on the CPU zones; raise the trip so
            # the locked frequency is never overridden). Hot shutdown trip
            # stays; mitigated by the 75% lock (lower heat).
            ("echo 115000 > /sys/class/thermal/thermal_zone0/trip_point_2_temp 2>/dev/null || true; "
             "echo 115000 > /sys/class/thermal/thermal_zone1/trip_point_2_temp 2>/dev/null || true; "
             "echo 115000 > /sys/class/thermal/thermal_zone2/trip_point_2_temp 2>/dev/null || true",
             "disable_thermal_downclock"),
            # Skip boot dexopt so dex2oat does not compete with the workload
            # (already-compiled apps keep their odex; only uncompiled boot
            # apps interpret).
            ("setprop pm.dexopt.boot skip 2>/dev/null || true", "dexopt_boot_skip"),
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

        # Cool down AFTER locking frequencies + screen-on prep: this is the
        # actual experiment condition (all cores at max freq), so the cooldown
        # result is the temperature the workload really starts from.
        f.write(f"[cool_down] start {datetime.now().isoformat()}\n")
        temps = wait_for_cool_down(serial, stop_event=stop_event)
        f.write(f"[cool_down] done BIG={temps.get('thermal_zone0', -1):.1f}°C "
                f"LITTLE={temps.get('thermal_zone2', -1):.1f}°C  "
                f"{datetime.now().isoformat()}\n")
        f.flush()

