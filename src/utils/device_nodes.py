"""sysctl / sysfs / procfs 节点 utils：设置 + 回读自检（仿 trace_utils 风格）。

供 memstress 后端 verify 使用：节点路径由配置（PARAMS.md 值列 `期望值@路径`）传入，
本模块只提供"写节点 / 读节点 / 设置后回读自检 / 只读自检"四个原子操作。
"""
from __future__ import annotations

from typing import Tuple

from exp_framework.utils import adb_utils


def read_node(serial: str, path: str) -> str:
    """读节点当前值：cat <path>，去 \r 与首尾空白。"""
    out = adb_utils.adb_shell_root(
        serial, f"cat {path}", timeout_s=15, tty=True, check=False)
    return (out or "").replace("\r", "").strip()


def set_node(serial: str, path: str, value) -> None:
    """写节点：echo <value> > <path>。"""
    adb_utils.adb_shell_root(
        serial, f"echo {value} > {path}", timeout_s=15, tty=True, check=False)


def set_and_verify_node(serial: str, path: str, expected) -> Tuple[bool, str]:
    """写期望值 → 回读 → 比较。返回 (ok, actual)。"""
    set_node(serial, path, expected)
    actual = read_node(serial, path)
    return actual == str(expected).strip(), actual


def verify_node(serial: str, path: str, expected) -> Tuple[bool, str]:
    """只读回读 → 比较（boot 参数等不可运行时设置的节点）。返回 (ok, actual)。"""
    actual = read_node(serial, path)
    return actual == str(expected).strip(), actual
