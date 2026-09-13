"""/proc/lock_stat snapshot + experiment-window delta (top-N by waittime).

lock_stat time values are in microseconds (kernel Documentation/locking/
lockstat.rst). A data row is: "<lock name>: <12 numeric columns>".
Rows with fewer fields (stack sections, headers, warnings) are skipped.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import adb_utils

_ROW_RE = re.compile(r"^\s*(\S+):\s+([\d.\s]+)$")


def read_lock_stat(serial: str) -> str:
    try:
        return adb_utils.adb_shell_root(serial, "cat /proc/lock_stat",
                                    timeout_s=30, tty=True, check=True)
    except Exception:
        return ""


def capture_lock_stat(serial: str, path: Path) -> str:
    """Snapshot /proc/lock_stat to `path` (raw) and return the text."""
    text = read_lock_stat(serial)
    path.write_text(text, encoding="utf-8")
    return text


def parse_lock_stat(text: str) -> Dict[str, List[float]]:
    """Return {lock_name: [12 numeric columns]} for data rows."""
    out: Dict[str, List[float]] = {}
    for line in text.splitlines():
        m = _ROW_RE.match(line)
        if not m:
            continue
        name = m.group(1).lstrip("&:")
        fields = m.group(2).split()
        if len(fields) != 12:
            continue
        try:
            out[name] = [float(f) for f in fields]
        except ValueError:
            continue
    return out


# column indices (0-based) in the 12-field row
COL_CONTENTIONS = 1
COL_WAITTIME_TOTAL = 4
COL_WAITTIME_AVG = 5
COL_HOLDTIME_TOTAL = 10


def lock_stat_delta(start_text: str, end_text: str, *,
                    top_n: int = 20) -> str:
    """First-vs-last snapshot deltas, top-N by waittime-total increment (ms)."""
    s = parse_lock_stat(start_text)
    e = parse_lock_stat(end_text)
    rows: List[Tuple[str, float, float, float, float]] = []
    for name, ev in e.items():
        sv = s.get(name)
        if sv is None:
            continue
        d_cont = ev[COL_CONTENTIONS] - sv[COL_CONTENTIONS]
        d_wait_ms = (ev[COL_WAITTIME_TOTAL] - sv[COL_WAITTIME_TOTAL]) / 1000.0
        avg_ms = ev[COL_WAITTIME_AVG] / 1000.0
        d_hold_ms = (ev[COL_HOLDTIME_TOTAL] - sv[COL_HOLDTIME_TOTAL]) / 1000.0
        if d_cont != 0 or abs(d_wait_ms) > 0.0001:
            rows.append((name, d_cont, d_wait_ms, avg_ms, d_hold_ms))
    rows.sort(key=lambda r: r[2], reverse=True)
    lines = [
        "lock_stat deltas (experiment window, ms): "
        "lock | contentions_delta | waittime_total_delta(ms) | "
        "waittime_avg(ms) | holdtime_total_delta(ms)",
    ]
    if not rows:
        lines.append("(no lock_stat rows with nonzero delta)")
        return "\n".join(lines)
    for name, dc, dw, av, dh in rows[:top_n]:
        lines.append(
            f"{name} | {dc:>10.0f} | {dw:>14.3f} | {av:>12.3f} | {dh:>16.3f}")
    return "\n".join(lines)
