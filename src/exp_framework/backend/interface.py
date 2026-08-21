"""memstress 实验后端：注册给 experiment-standard 前端的公共接口实现。

接口三件套（用户确认的最终形态）：
  parse(extracted, probes)      —— 前端提取数据 → 完整 manifest（直白赋值 + 逗号节点拆分 + 域分流）
  verify(config)                —— 串调六个 xx_utils.verify(config)，每项含 actual，打印 + 返回
  list_log_paths(config, out_dir) —— 日志路径总集 [{name, path, kind}]（kind: sample|log）

节点参数约定：PARAMS.md 值列 `节点路径,期望值`（逗号分隔，路径在前）。
域列分流：含"内核命令行/boot/cmdline" → boot_params（kernel_boot_utils 处理，
pixel 重打包 vendor_boot / cuttlefish 查 cmdline）；其余 → sysctl_nodes（sysctl_util 设置+回读）。
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from exp_framework.utils import pkg_utils  # noqa: E402
from exp_framework.utils import sysctl_util  # noqa: E402
from exp_framework.utils import tasktime_utils  # noqa: E402
from exp_framework.utils import kernel_boot_utils  # noqa: E402
from exp_framework.utils import trace_utils  # noqa: E402
from exp_framework.utils import vmstat_utils  # noqa: E402

BACKEND: Dict[str, Any] = {
    "name": "memstress",
    "domains": ["pixel", "cuttlefish"],
}

_BOOT_DOMAIN_TOKENS = ("内核命令行", "boot", "cmdline")
_KNOWN_PARAMS = frozenset((
    "max_cycles", "interval_s", "no_network_check",
    "burst_size", "hold_ms", "launch_gap_ms", "cycle_sleep_ms", "seed",
    "packages", "trace_events", "trace_strict", "vmstat_keys",
    "tasktime_procs", "buddyinfo_interval_s", "vmstat_interval_s",
    "lock_stat_enabled",
    "power_odpm", "power_batterystats",
))


def _to_int(s: str) -> int:
    return int(str(s).strip())


def _to_float(s: str) -> float:
    return float(str(s).strip())


def _to_bool(s: str) -> bool:
    return str(s).strip().lower() == "true"


def _to_json(s: str):
    s = str(s).strip()
    return None if not s else __import__("json").loads(s)


# ------------------------------------------------------------------ parse


def parse(extracted: Dict[str, str],
          probes: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """前端提取的 {参数:字符串} → 完整 manifest dict（直白赋值，无路由表无模板）。

    - 已知参数：类型转换后直接赋值到 config/sample_config 对应位置。
    - 未知参数且值含逗号（非 JSON 字面量）→ 节点项：
        `路径,期望值` 拆分，域含 boot 标记 → boot_params，否则 → sysctl_nodes。
    - 未知且非节点：跳过并告警。
    """
    domain_by_param = {p["param"]: p.get("domain", "") for p in (probes or [])}
    manifest: Dict[str, Any] = {
        "config": {"memstress": {}},
        "sample_config": {},
        "sysctl_nodes": [],
        "boot_params": [],
    }
    for param, raw in extracted.items():
        s = str(raw).strip()
        if param not in _KNOWN_PARAMS and "," in s \
                and not s.startswith("[") and not s.startswith("{"):
            path, _, expected = s.partition(",")
            item = {"param": param, "path": path.strip(),
                    "expected": expected.strip()}
            domain = domain_by_param.get(param, "")
            if any(tok in domain for tok in _BOOT_DOMAIN_TOKENS):
                manifest["boot_params"].append(item)
            else:
                manifest["sysctl_nodes"].append(item)
            continue

        cfg = manifest["config"]
        scfg = manifest["sample_config"]
        if param == "max_cycles":
            cfg["max_cycles"] = _to_int(raw)
        elif param == "interval_s":
            cfg["interval_s"] = _to_int(raw)
        elif param == "no_network_check":
            cfg["no_network_check"] = _to_bool(raw)
        elif param == "burst_size":
            cfg["memstress"]["burst_size"] = _to_int(raw)
        elif param == "hold_ms":
            cfg["memstress"]["hold_ms"] = _to_int(raw)
        elif param == "launch_gap_ms":
            cfg["memstress"]["launch_gap_ms"] = _to_int(raw)
        elif param == "cycle_sleep_ms":
            cfg["memstress"]["cycle_sleep_ms"] = _to_int(raw)
        elif param == "seed":
            cfg["memstress"]["seed"] = _to_int(raw)
        elif param == "packages":
            cfg["memstress"]["packages"] = _to_json(raw)
        elif param == "trace_events":
            data = _to_json(raw) or []
            if data and all(isinstance(e, str) for e in data):
                data = [{"name": "frontend", "events": data}]
            scfg.setdefault("trace", {})["captures"] = data
        elif param == "trace_strict":
            scfg.setdefault("trace", {})["strict"] = _to_bool(raw)
        elif param == "vmstat_keys":
            scfg.setdefault("vmstat", {})["keys"] = _to_json(raw)
        elif param == "vmstat_interval_s":
            scfg.setdefault("vmstat", {})["interval_s"] = _to_int(raw)
        elif param == "buddyinfo_interval_s":
            scfg.setdefault("vmstat", {}).setdefault("buddyinfo", {})["interval_s"] = _to_int(raw)
        elif param == "tasktime_procs":
            scfg.setdefault("tasktime", {})["procs"] = _to_json(raw)
        elif param == "lock_stat_enabled":
            scfg.setdefault("lock_stat", {})["enabled"] = _to_bool(raw)
        elif param == "power_odpm":
            scfg.setdefault("power", {})["odpm"] = _to_bool(raw)
        elif param == "power_batterystats":
            scfg.setdefault("power", {})["batterystats"] = _to_bool(raw)
        else:
            print(f"parse: 跳过未知参数 {param}={raw!r}", file=sys.stderr)
    return manifest


# ------------------------------------------------------------------ verify


def verify(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按类别串调六个 utils 的 verify(config)，每项含 actual，已当场打印。"""
    results: List[Dict[str, Any]] = []
    results += sysctl_util.verify(config)
    results += kernel_boot_utils.verify(config)
    results += trace_utils.verify(config)
    results += vmstat_utils.verify(config)
    results += tasktime_utils.verify(config)
    results += pkg_utils.verify(config)
    return results


# ------------------------------------------------------------------ list_log_paths


def list_log_paths(config: Dict[str, Any], out_dir: str) -> List[Dict[str, Any]]:
    """本次运行的日志路径总集（含即将产生）：[{name, path, kind}]，kind: sample|log。"""
    d = str(out_dir)
    items: List[Dict[str, Any]] = [
        {"name": "raw样本", "path": f"{d}/raw_samples.csv", "kind": "sample"},
        {"name": "derived", "path": f"{d}/derived.csv", "kind": "sample"},
        {"name": "summary", "path": f"{d}/summary.md", "kind": "log"},
        {"name": "run_manifest", "path": f"{d}/run_manifest.json", "kind": "log"},
        {"name": "设备准备", "path": f"{d}/device_prepare_log.txt", "kind": "log"},
        {"name": "memstress轮次", "path": f"{d}/memstress/cycle_log.jsonl", "kind": "sample"},
        {"name": "轮次计时", "path": f"{d}/memstress/cycle_timing.json", "kind": "sample"},
        {"name": "设备cycles", "path": f"{d}/memstress/device_cycles.tsv", "kind": "sample"},
        {"name": "设备events", "path": f"{d}/memstress/device_events.tsv", "kind": "sample"},
        {"name": "设备端心跳", "path": f"{d}/memstress/runner_self.log", "kind": "log"},
        {"name": "hang快照", "path": f"{d}/memstress/hang_snapshot.log", "kind": "log"},
    ]
    caps = (config.get("sample_config") or {}).get("trace", {}).get("captures", [])
    for cap in caps:
        name = cap.get("name") or "main"
        items.append({"name": f"trace-{name}",
                      "path": f"{d}/trace_{name}.txt", "kind": "sample"})
    return items
