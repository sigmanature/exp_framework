"""sysctl / sysfs / procfs 节点：设置 + 回读自检（launch 脚本 REQS 数组逻辑搬运）。

verify(config) 输入 config 约定：
  config["sysctl_nodes"] = [{"param", "path", "expected"}]
  config["exp_ctx"] = {"serial"}
比对规则：先等值；期望值以 "[" 开头时前缀匹配（Android sysfs 激活项标记格式）。
每项返回 {"param", "expected", "actual", "ok"} 并当场打印一行（对齐 launch 输出）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from exp_framework.utils import device_nodes


def _match(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    # 方括号期望值（如 [always]）表示"激活标记"，显示格式不保证在首位
    # （anon_enabled_show 只有 [always] 在首位，[inherit]/[madvise]/[never]
    #  都在中间或末尾）——用包含匹配，括号标记在字符串里唯一。
    if expected.startswith("[") and expected in actual:
        return True
    return False


def _write_value(expected: str) -> str:
    """写入值剥掉方括号显示标记：内核按无括号值解析（[never] -> never）。"""
    if expected.startswith("[") and expected.endswith("]"):
        return expected[1:-1]
    return expected


def verify(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    ctx = config.get("exp_ctx", {})
    serial = ctx.get("serial")
    nodes = config.get("sysctl_nodes", [])
    results: List[Dict[str, Any]] = []
    for n in nodes:
        path, expected = n["path"], n["expected"]
        device_nodes.set_node(serial, path, _write_value(expected))
        actual = device_nodes.read_node(serial, path)
        ok = _match(actual, expected)
        print(f"  {n['param']:<28s} = {actual!r:<38s} "
              f"[{'OK' if ok else 'MISMATCH(期望=' + expected + ')'}]")
        results.append({"param": n["param"], "expected": expected,
                        "actual": actual, "ok": ok})
    return results
