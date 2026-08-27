#!/usr/bin/env python3
"""hot-hook 版实验锁门禁（tool.execute.before）。

opencode 项目级 .opencode/plugins/exp-lock-gate 只在 exp_framework 为
工作区时加载；实际实验通常在 learn_os 等目录跑，门禁失效。本脚本通过
全局 hot-hook 注册 tool.execute.before，任何工作区执行 bash 工具前
都会先查 exp_lock 游标：

- 检查类/标准入口命令 → 放行
- 启动类命令（setsid/nohup/runner 等）且设备 running/cleanup_failed
  → 阻止执行（stderr 打印占用者与正确流程）

stdin 契约：opencode hook 输入 JSON（含 tool / input.command）。
"""
import json
import subprocess
import sys

GATE = "/home/nzzhao/skills-repos/exp_framework/scripts/lock_gate.py"


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("tool") != "bash":
        return 0
    cmd = (data.get("input") or {}).get("command") or ""
    if not cmd:
        return 0
    try:
        r = subprocess.run(["python3", GATE, "--cmd", cmd],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return 0
    if r.returncode != 0:
        print(r.stderr or "设备被占用，禁止裸命令启实验", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
