#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_framework 工具门禁（opencode tool.execute.before hook）。

拦截"在实验设备上启进程/启实验/启冒烟"的裸命令——模型本能生成的
bash 会被拒绝，必须走项目标准流程（runner 会自己 exp_lock_claim）。

判定：
- 检查类命令（ps/cat/grep/tail/ls/adb devices/echo 只读等）→ 放行
- 命令命中"设备操作/启进程"模式 → 查 exp_lock 游标：
    state=running/cleanup_failed → 拒绝并提示占用者与正确流程
    空闲 → 放行（runner 启动时会自己 claim，双保险）
- 仅在本项目（exp_framework 仓库）内通过 .opencode/opencode.json 注册生效。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 检查类命令（只读/查看，放行）
READ_ONLY_PATTERNS = [
    r"^\s*(adb devices|adb\s+-\S+\s+(devices|logcat))",
    r"^\s*(adb\s+-\S+\s+shell\s+(ps|cat|grep|ls|dumpsys|getprop|wc|date|echo|id|uptime|top)\b)",
    r"^\s*(ps|pgrep|grep|rg|cat|tail|head|ls|stat|find|wc|du|df|git|python3 -m pytest|python3 -m py_compile)",
]

# 启动类命令模式（在设备上启进程/启实验/启冒烟）
START_PATTERNS = [
    r"setsid",
    r"nohup",
    r"\b(at|cron)\b",
    r"screen\s+-",
    r"tmux\s+(new|new-session)",
    r"adb\s+-\S+\s+shell\s+(setsid|nohup|sh\s+-c|chmod.*&&)",
    r"python3\s+.*(run_memstress_and_collect_logs|experiment/runner|\.py).*--serial",
    r"python3\s+-m\s+experiment",
]

EXEMPT_PREFIXES = [  # 标准流程白名单（runner 自锁，放行）
    "python3 -m pytest",
    "python3 -m py_compile",
    "python3 scripts/selfcheck/",
]


def _is_read_only(cmd: str) -> bool:
    return any(re.search(p, cmd) for p in READ_ONLY_PATTERNS)


def _is_standard_entry(cmd: str) -> bool:
    return any(cmd.strip().startswith(p) for p in EXEMPT_PREFIXES)


def _is_start_cmd(cmd: str) -> bool:
    return any(re.search(p, cmd) for p in START_PATTERNS)


def _lock_status() -> dict:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from exp_framework.utils.exp_lock import exp_lock_list, exp_lock_status
        return {"list": exp_lock_list(), "rows": [exp_lock_status(*k.split(":", 1))
                                                 for k in exp_lock_list().get("entries", {})]}
    except Exception as e:
        return {"error": str(e)}


def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--cmd", default="")
    p.add_argument("--check-lock", action="store_true",
                   help="仅输出当前 exp_lock 状态（供检查类命令使用）")
    args = p.parse_args()

    if args.check_lock:
        st = _lock_status()
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return 0

    cmd = args.cmd.strip()
    if not cmd:
        return 0

    if _is_standard_entry(cmd) or _is_read_only(cmd):
        return 0

    if _is_start_cmd(cmd):
        sys.path.insert(0, str(REPO_ROOT / "src"))
        try:
            from exp_framework.utils.exp_lock import exp_lock_list
        except Exception:
            return 0  # 锁模块不可用时放行（不阻塞开发）
        busy = []
        for key, e in (exp_lock_list().get("entries", {}) or {}).items():
            if e.get("state") in ("running", "cleanup_failed"):
                busy.append(f"({key}) {e.get('state')} exp={e.get('exp_id')} "
                            f"session={e.get('session_id') or 'manual'}")
        if busy:
            print(f"[lock_gate] 拒绝：设备上存在占用实验，禁止裸命令启实验。\n"
                  f"占用: {'; '.join(busy)}\n"
                  f"正确流程: 走 exp_framework 标准入口（runner 自动 exp_lock_claim），"
                  f"或查询 exp_lock_status / exp_lock_poll_until_free 后排队。",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
