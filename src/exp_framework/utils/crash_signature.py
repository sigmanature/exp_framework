from __future__ import annotations

import json
import re
import time
from collections import deque
from pathlib import Path
from typing import Deque, Iterable, Optional, Tuple


class TargetCrashSignatureDetector:
    """Detect target-package classloading crashes from logcat lines.

    Only triggers when an am_crash event from a target package has a
    ClassNotFoundException / NoClassDefFoundError / ClassNotFoundError as
    its exception type. Cross-process proximity no longer counts.
    """

    _AM_CRASH_RE = re.compile(
        r"\bam_crash:\s*\["
        r"[^,]+,"       # pid
        r"[^,]+,"       # uid
        r"([^,]+),"     # package (group 1)
        r"[^,]+,"       # flags
        r"([^,]+),"     # exception type (group 2)
    )
    _CNFE_TYPES = {"ClassNotFoundException", "NoClassDefFoundError", "ClassNotFoundError"}

    def __init__(self, *, serial: str, target_packages: Iterable[str], window_lines: int = 500) -> None:
        self.serial = serial
        self.target_packages = set(target_packages)
        self.window_lines = max(1, int(window_lines))
        self.context: Deque[str] = deque(maxlen=200)

    def process_line(self, line: str) -> Optional[dict]:
        s = line.rstrip("\n")
        self.context.append(s)

        pkg, exc_type = self._parse_am_crash(s)
        if not pkg or pkg not in self.target_packages:
            return None
        if not any(t in exc_type for t in self._CNFE_TYPES):
            return None

        return {
            "serial": self.serial,
            "host_ts": int(time.time()),
            "reason": "target package am_crash with classloading exception",
            "matched_line": s,
            "matched_package": pkg,
            "exception_type": exc_type,
            "context_tail": list(self.context),
        }

    def write_payload(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _parse_am_crash(self, line: str) -> Tuple[str, str]:
        m = self._AM_CRASH_RE.search(line)
        if not m:
            return "", ""
        return m.group(1).strip(), m.group(2).strip()
