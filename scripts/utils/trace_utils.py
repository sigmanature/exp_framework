"""Device-side ftrace capture probe (parallel per-buffer readers).

Design (aligned with the frag20ms probe pattern):
  - host deploys a probe script that spawns ONE background loop per buffer
    (main buffer or an instance), each blocking-reads its trace_pipe into a
    device-side file. No adb dependency while capturing: adb disconnects do
    not interrupt sampling.
  - events/instance/buffer configuration is done by the host before start
    (single-shot adb calls).
  - stop uses a STOP marker file: every reader loop exits when it appears;
    host then pulls each <name>.trace into <out>/trace_<name>.txt.

"Some events in a dedicated buffer" == one probe entry with an instance name
(ftrace has no per-event buffers; instances are the official mechanism).
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from utils import adb_utils

PROBE_DIR = "/data/local/tmp/trace_capture"
PROBE_SCRIPT = f"{PROBE_DIR}/probe.sh"
STOP_FILE = f"{PROBE_DIR}/stop"
HEARTBEAT_FILE = f"{PROBE_DIR}/heartbeat"
HEARTBEAT_INTERVAL_S = 30
HEARTBEAT_TTL_S = 60
TRACE_ROOT = "/sys/kernel/tracing"
MAIN_NAME = "main"

_heartbeat_thread = None
_heartbeat_stop = threading.Event()


# ---------------------------------------------------------------- adb helpers

def _adb(serial: str, cmd: str, timeout_s: int = 30, retries: int = 3) -> str:
    """adb shell with retries: only retry when the command itself failed
    (non-zero rc); a success with empty output is a normal fast path and is
    returned immediately (no blind retries)."""
    last = ""
    for attempt in range(max(1, retries)):
        cp = subprocess.run(["adb", "-s", serial, "shell", cmd],
                            capture_output=True, text=True, timeout=timeout_s)
        if cp.returncode == 0:
            return (cp.stdout or "") + (cp.stderr or "")
        last = (cp.stdout or "") + (cp.stderr or "")
        time.sleep(1 + attempt)
    return last


def _adb_ok(serial: str, cmd: str, timeout_s: int = 30) -> bool:
    cp = subprocess.run(["adb", "-s", serial, "shell", cmd],
                        capture_output=True, text=True, timeout=timeout_s)
    return cp.returncode == 0


def _adb_root(serial: str, cmd: str, timeout_s: int = 30) -> str:
    """权限命令（root/su 自动），见 adb_utils.adb_shell_root。"""
    return adb_utils.adb_shell_root(serial, cmd, timeout_s=timeout_s, check=False)


def base_path(instance: Optional[str]) -> str:
    """tracefs path for a buffer: main root or instances/<name>."""
    if not instance or instance == MAIN_NAME:
        return TRACE_ROOT
    return f"{TRACE_ROOT}/instances/{instance}"


# ------------------------------------------------------------ tracefs config

def validate_events(serial: str, events: Sequence[str],
                    strict: bool = True) -> List[str]:
    """Check events exist in available_events. Returns missing list;
    raises RuntimeError when strict and anything is missing."""
    out = _adb_root(serial, f"cat {TRACE_ROOT}/available_events", timeout_s=30)
    available = set(out.split())
    missing = [e for e in events if e not in available]
    if missing and strict:
        raise RuntimeError(f"trace events not available on device: {missing}")
    return missing


def reset_buffer(serial: str, instance: Optional[str]) -> None:
    """tracing off + clear trace + clear set_event for a buffer."""
    base = base_path(instance)
    _adb_root(serial, f"echo 0 > {base}/tracing_on; "
                      f"echo > {base}/trace; echo > {base}/set_event")


def enable_events(serial: str, events: Sequence[str],
                  instance: Optional[str]) -> None:
    base = base_path(instance)
    joined = " ".join(events)
    _adb_root(serial, f'echo "{joined}" > {base}/set_event')


def set_event_pid(serial: str, pids: Sequence[int],
                  instance: Optional[str]) -> None:
    """Restrict events of a buffer to the given PIDs (sched analysis)."""
    base = base_path(instance)
    joined = " ".join(str(p) for p in pids) if pids else ""
    _adb_root(serial, f'echo "{joined}" > {base}/set_event_pid')


def set_buffer_kb(serial: str, instance: Optional[str], kb: int) -> None:
    base = base_path(instance)
    _adb_root(serial, f"echo {max(1, kb)} > {base}/buffer_size_kb")


def tracing_on(serial: str, instance: Optional[str], on: bool) -> None:
    base = base_path(instance)
    _adb_root(serial, f"echo {1 if on else 0} > {base}/tracing_on")


def set_trace_clock_mono(serial: str, instance: Optional[str]) -> None:
    """Pin the buffer clock to monotonic so cross-CPU timestamps are comparable."""
    base = base_path(instance)
    _adb_root(serial, f"echo mono > {base}/trace_clock")


def create_instance(serial: str, name: str) -> None:
    _adb_root(serial, f"mkdir -p {TRACE_ROOT}/instances/{name}")


def remove_instance(serial: str, name: str) -> None:
    _adb_root(serial, f"rmdir {TRACE_ROOT}/instances/{name}")


# ------------------------------------------------------------- probe lifecycle

def deploy_trace_probe(serial: str, captures: Sequence[Dict]) -> None:
    """Generate probe.sh (one background reader loop per capture) and push it."""
    reader_lines: List[str] = []
    for cap in captures:
        name = cap.get("name") or MAIN_NAME
        out = f"{PROBE_DIR}/{name}.trace"
        if name == MAIN_NAME:
            base = TRACE_ROOT
        else:
            base = f"{TRACE_ROOT}/instances/{name}"
        reader_lines.append(
            f"( while [ ! -e \"$STOP\" ]; do "
            f"timeout 2 cat {base}/trace_pipe >> {out}; done ) &")
        reader_lines.append(f"echo $! >> {PROBE_DIR}/readers.pid")
    readers = "\n".join(reader_lines)
    script = f"""#!/system/bin/sh
# auto-generated trace capture probe (parallel per-buffer readers)
PROBE_DIR={PROBE_DIR}
STOP={STOP_FILE}
HB={HEARTBEAT_FILE}
rm -f "$STOP" {PROBE_DIR}/readers.pid
touch "$HB"
{readers}
# exit on STOP marker, or on stale heartbeat (host died / host aborted):
# bounded reader reads (timeout 2) guarantee this loop is re-checked often
while [ ! -e "$STOP" ]; do
  if [ -f "$HB" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$HB" 2>/dev/null || echo 0) ))
    [ "$age" -gt {HEARTBEAT_TTL_S} ] && break
  fi
  sleep 1
done
"""
    Path("/tmp/opencode/trace_probe_gen.sh").write_text(script, encoding="utf-8")
    _adb_root(serial, f"mkdir -p {PROBE_DIR}; chmod 777 {PROBE_DIR}")
    cp = subprocess.run(["adb", "-s", serial, "push",
                         "/tmp/opencode/trace_probe_gen.sh", PROBE_SCRIPT],
                        capture_output=True, text=True, timeout=60)
    if cp.returncode != 0:
        raise RuntimeError(f"trace probe push failed: {cp.stderr}")
    _adb_root(serial, f"chmod 755 {PROBE_SCRIPT}")


def _heartbeat_loop(serial: str) -> None:
    """Host-side keep-alive: refresh the probe heartbeat file every
    HEARTBEAT_INTERVAL_S. Dies with the host process (daemon thread); if the
    host is killed/aborted the probe self-exits after HEARTBEAT_TTL_S."""
    while not _heartbeat_stop.wait(HEARTBEAT_INTERVAL_S):
        _adb_root(serial, f"touch {HEARTBEAT_FILE}", timeout_s=15)


def start_trace_probe(serial: str, captures: Sequence[Dict],
                      ready_timeout_s: int = 20) -> None:
    """Configure buffers/events, then launch the probe (setsid) and wait ready.
    On any failure after launch, stop the probe (STOP marker) before
    propagating so no device-side reader is left behind."""
    global _heartbeat_thread, _heartbeat_stop
    for cap in captures:
        name = cap.get("name") or MAIN_NAME
        events = list(cap.get("events", []))
        kb = int(cap.get("buffer_kb", 1024))
        if name != MAIN_NAME:
            create_instance(serial, name)
        reset_buffer(serial, name)
        set_trace_clock_mono(serial, name)
        # drop stale device-side output from previous runs (probe appends)
        _adb_root(serial, f"rm -f {PROBE_DIR}/{name}.trace")
        if events:
            enable_events(serial, events, name)
        set_buffer_kb(serial, name, kb)
        tracing_on(serial, name, True)
    try:
        _adb_root(serial, f"(setsid sh {PROBE_SCRIPT} </dev/null >/dev/null 2>&1 &)")
        _heartbeat_stop = threading.Event()
        _heartbeat_thread = threading.Thread(
            target=_heartbeat_loop, args=(serial,), daemon=True)
        _heartbeat_thread.start()
        deadline = time.monotonic() + ready_timeout_s
        while time.monotonic() < deadline:
            ready = True
            for cap in captures:
                name = cap.get("name") or MAIN_NAME
                if not _adb_ok(serial, f"test -f {PROBE_DIR}/{name}.trace",
                               timeout_s=10):
                    ready = False
                    break
            if ready:
                return
            time.sleep(1)
        raise RuntimeError("trace probe did not become ready "
                           f"(expected files under {PROBE_DIR})")
    except Exception:
        _stop_heartbeat()
        # stop the probe that may already be running, wait for its self-exit
        # (bounded reads), then remove marker files so no state is left over
        _adb_root(serial, f"touch {STOP_FILE}", timeout_s=15)
        time.sleep(3)
        _adb_root(serial, f"rm -f {STOP_FILE} {PROBE_DIR}/readers.pid",
                  timeout_s=15)
        raise


def _stop_heartbeat() -> None:
    global _heartbeat_thread
    _heartbeat_stop.set()
    if _heartbeat_thread is not None:
        _heartbeat_thread.join(timeout=HEARTBEAT_INTERVAL_S + 5)
        _heartbeat_thread = None


def stop_trace_probe(serial: str, captures: Sequence[Dict], out_dir: Path,
                     wait_timeout_s: int = 15) -> None:
    """Stop readers: STOP marker + tracing off (stops new events). Readers use
    bounded reads and exit by themselves within ~2s of the marker appearing."""
    _stop_heartbeat()
    _adb_root(serial, f"touch {STOP_FILE}")
    # stop new events; readers drain remaining data within their bounded reads
    tracing_on(serial, None, False)
    for cap in captures:
        name = cap.get("name") or MAIN_NAME
        tracing_on(serial, name, False)
    deadline = time.monotonic() + wait_timeout_s
    while time.monotonic() < deadline:
        alive = False
        for pid_line in _adb_root(serial, f"cat {PROBE_DIR}/readers.pid 2>/dev/null",
                                   timeout_s=10).split():
            if pid_line.isdigit() and _adb_ok(serial, f"kill -0 {pid_line}",
                                              timeout_s=10):
                alive = True
                break
        if not alive:
            break
        time.sleep(1)
    for cap in captures:
        name = cap.get("name") or MAIN_NAME
        remote = f"{PROBE_DIR}/{name}.trace"
        local = out_dir / f"trace_{name}.txt"
        subprocess.run(["adb", "-s", serial, "pull", remote, str(local)],
                       capture_output=True, timeout=60)
    _adb_root(serial, f"rm -f {STOP_FILE} {PROBE_DIR}/readers.pid")


# ------------------------------------------------------------------ gate 预检

def verify(config: dict) -> list:
    """trace 事件名校验（gate 预检）：复用 validate_events(strict=False)。

    config 约定：config["sample_config"]["trace"]["captures"][].events，
    config["_ctx"] = {"serial"}。
    """
    from typing import Any, Dict, List
    ctx = config.get("_ctx", {})
    serial = ctx.get("serial")
    captures = (config.get("sample_config") or {}).get("trace", {}).get("captures", [])
    events = [e for cap in captures for e in (cap.get("events") or [])]
    if not events:
        return []
    missing = validate_events(serial, events, strict=False)
    print(f"  {'trace_events':<28s} = {len(events) - len(missing)}/{len(events)} 可用 "
          f"[{'OK' if not missing else 'MISMATCH(缺 ' + str(missing) + ')'}]")
    return [{"param": "trace_events",
             "expected": f"{len(events)} 个事件可用",
             "actual": f"缺 {len(missing)} 个",
             "ok": not missing}]
