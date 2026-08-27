#!/usr/bin/env python3
"""hot-hook 版实验锁门禁（tool.execute.before）。

opencode 项目级 .opencode/plugins/exp-lock-gate 只在 exp_framework 为
工作区时加载；实际实验通常在 learn_os 等目录跑，门禁失效。本脚本通过
全局 hot-hook 注册 tool.execute.before，任何工作区执行 bash 工具前
都会先查 exp_lock 游标：

- 检查类/标准入口命令 → 放行
- 启动类命令（setsid/nohup/runner 等）且设备 running/cleanup_failed
  → 阻止执行（stderr 打印占用者与正确流程）

协议：stdin 接收一次 JSON 请求，stdout 返回一次 JSON 决策。
"""
import json
import subprocess
import sys

GATE = "/home/nzzhao/skills-repos/exp_framework/scripts/lock_gate.py"
PROTOCOL_VERSION = 1


def _emit(decision: str, reason: str | None = None) -> int:
    payload = {"version": PROTOCOL_VERSION, "decision": decision}
    if reason:
        payload["reason"] = reason
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def main() -> int:
    if sys.stdin.isatty():
        return _emit("allow", "interactive stdin is not a hook request")
    try:
        data = json.load(sys.stdin)
    except Exception as error:
        return _emit("allow", f"invalid hook request: {error}")
    if not isinstance(data, dict):
        return _emit("allow", "invalid hook request: expected object")
    if data.get("version") != PROTOCOL_VERSION:
        return _emit("allow", "unsupported hook protocol version")
    if data.get("hook") != "tool.execute.before" or data.get("tool") != "bash":
        return _emit("allow")
    hook_input = data.get("input")
    if not isinstance(hook_input, dict):
        return _emit("allow", "invalid hook input: expected object")
    cmd = hook_input.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return _emit("allow")
    try:
        r = subprocess.run(["python3", GATE, "--cmd", cmd],
                           capture_output=True, text=True, timeout=10)
    except Exception as error:
        return _emit("allow", f"lock gate check failed: {error}")
    if r.returncode != 0:
        return _emit("deny", r.stderr.strip() or "设备被占用，禁止裸命令启实验")
    return _emit("allow")


if __name__ == "__main__":
    sys.exit(main())
