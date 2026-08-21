"""Host-side helper to deploy and run fragmem on an Android device.

Provides:
  - push_fragmem(): push the binary to device
  - start_fragmem(): launch fragmem in background, wait for FRAGMEM_READY
  - stop_fragmem(): kill fragmem on device
  - run_fragmem_precondition(): full workflow (push + start + verify + return)
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional


FRAGMEM_BINARY = Path(__file__).resolve().parent / "fragmem" / "fragmem"
DEVICE_PATH = "/data/local/tmp/fragmem"

# Default parameters
DEFAULT_ALLOC_MB = 4000
DEFAULT_CHUNK_KB = 4
DEFAULT_STRIDE = 2
DEFAULT_THRESHOLD = 2000


def push_fragmem(serial: str, binary: Optional[Path] = None) -> None:
    """Push the fragmem binary to the device."""
    src = binary or FRAGMEM_BINARY
    if not src.exists():
        raise FileNotFoundError(f"fragmem binary not found: {src}")

    subprocess.run(
        ["adb", "-s", serial, "push", str(src), DEVICE_PATH],
        capture_output=True, check=True, timeout=30,
    )
    subprocess.run(
        ["adb", "-s", serial, "shell", "chmod", "755", DEVICE_PATH],
        capture_output=True, check=True, timeout=10,
    )


def start_fragmem(
    serial: str,
    *,
    alloc_mb: int = DEFAULT_ALLOC_MB,
    chunk_kb: int = DEFAULT_CHUNK_KB,
    stride: int = DEFAULT_STRIDE,
    threshold: int = DEFAULT_THRESHOLD,
    zone: str = "Normal",
    min_order: int = 2,
    timeout_s: int = 120,
    quiet: bool = False,
) -> dict:
    """Start fragmem on device and wait for FRAGMEM_READY.

    Returns a dict with parsed ready line fields:
      - alloc_mb, held_mb, sum_order2, threshold, pid
    """
    cmd_parts = [
        DEVICE_PATH,
        f"--alloc-mb {alloc_mb}",
        f"--chunk-kb {chunk_kb}",
        f"--stride {stride}",
        f"--threshold {threshold}",
        f"--zone {zone}",
        f"--min-order {min_order}",
    ]
    if quiet:
        cmd_parts.append("--quiet")

    inner_cmd = " ".join(cmd_parts)

    # Run fragmem in background on device (root/su 自动), redirect stdout to a
    # ready file, then poll that file.
    ready_file = "/data/local/tmp/fragmem_ready.txt"
    launch_cmd = f"rm -f {ready_file}; {inner_cmd} > {ready_file} 2>/data/local/tmp/fragmem.log &"
    from utils import adb_utils
    adb_utils.adb_shell_root(serial, launch_cmd, timeout_s=15, check=False)

    # Poll for FRAGMEM_READY
    t0 = time.time()
    result = {}
    while time.time() - t0 < timeout_s:
        time.sleep(1)
        cp = subprocess.run(
            ["adb", "-s", serial, "shell", f"cat {ready_file} 2>/dev/null"],
            capture_output=True, text=True, timeout=10,
        )
        line = cp.stdout.strip()
        if "FRAGMEM_READY" in line:
            result = _parse_ready_line(line)
            # Get PID
            pid_cp = subprocess.run(
                ["adb", "-s", serial, "shell", "pidof fragmem"],
                capture_output=True, text=True, timeout=10,
            )
            pid_str = pid_cp.stdout.strip()
            result["pid"] = int(pid_str) if pid_str.isdigit() else -1
            break
    else:
        # Timeout — check if process is running at all
        pid_cp = subprocess.run(
            ["adb", "-s", serial, "shell", "pidof fragmem"],
            capture_output=True, text=True, timeout=10,
        )
        raise TimeoutError(
            f"fragmem did not produce FRAGMEM_READY within {timeout_s}s. "
            f"pid={pid_cp.stdout.strip()}"
        )

    return result


def _parse_ready_line(line: str) -> dict:
    """Parse 'FRAGMEM_READY alloc_mb=3000 held_mb=1500 sum_order2=1234 threshold=2000'"""
    result = {}
    parts = line.split()
    for part in parts[1:]:  # skip "FRAGMEM_READY"
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                result[k] = int(v)
            except ValueError:
                result[k] = v
    return result


def stop_fragmem(serial: str) -> None:
    """Kill fragmem on the device."""
    from utils import adb_utils
    adb_utils.adb_shell_root(
        serial, "killall fragmem 2>/dev/null; rm -f /data/local/tmp/fragmem_ready.txt",
        timeout_s=10, check=False)


def is_fragmem_running(serial: str) -> bool:
    """Check if fragmem is running on device."""
    cp = subprocess.run(
        ["adb", "-s", serial, "shell", "pidof fragmem"],
        capture_output=True, text=True, timeout=10,
    )
    return cp.stdout.strip().isdigit()


def run_fragmem_precondition(
    serial: str,
    *,
    alloc_mb: int = DEFAULT_ALLOC_MB,
    chunk_kb: int = DEFAULT_CHUNK_KB,
    stride: int = DEFAULT_STRIDE,
    threshold: int = DEFAULT_THRESHOLD,
    zone: str = "Normal",
    min_order: int = 2,
    timeout_s: int = 120,
    quiet: bool = False,
) -> dict:
    """Full preconditioning workflow: push binary, start, wait for ready.

    Returns the parsed result dict from start_fragmem().
    """
    if not quiet:
        print(f"[fragmem] pushing binary to device...")
    push_fragmem(serial)

    # Kill any existing instance
    stop_fragmem(serial)
    time.sleep(1)

    if not quiet:
        print(f"[fragmem] starting: alloc={alloc_mb}MB chunk={chunk_kb}KB "
              f"stride={stride} threshold={threshold}")

    result = start_fragmem(
        serial,
        alloc_mb=alloc_mb,
        chunk_kb=chunk_kb,
        stride=stride,
        threshold=threshold,
        zone=zone,
        min_order=min_order,
        timeout_s=timeout_s,
        quiet=quiet,
    )

    if not quiet:
        print(f"[fragmem] READY: held={result.get('held_mb', '?')}MB "
              f"sum_order{min_order}={result.get(f'sum_order{min_order}', '?')} "
              f"pid={result.get('pid', '?')}")

    return result
