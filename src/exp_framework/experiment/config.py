"""配置加载与解析（输入 config -> 后端参数 + 采样配置），运行时清单写入。

术语：
  config          输入配置（用户修改的参数），见 config/*.json
  run_manifest.json  运行时清单（框架生成的结果档案，输出到实验目录）

config 顶层结构：
{
  "serial": "21121FDF600C4G",
  "config": {
    "counters": [...],
    "interval_s": 60,
    "backend": {"name": "memstress", "config": { ...后端私有参数... }}
  },
  "sample_config": {
    "vmstat": {"keys": [...], "interval_s": 60,
               "buddyinfo": {"enabled": false, "interval_s": 0}},
    "tasktime": {...}, "trace": {...}, "lock_stat": {...}, "power": {...}
  }
}

采样间隔统一在 sample_config 配置（vmstat.interval_s / vmstat.buddyinfo.interval_s），
config 层不再有 vmstat_interval_s / buddyinfo_interval_s 字段。
buddyinfo 属于 vmstat 采样的子项，默认不采集（enabled=false）。
"""
import json
from pathlib import Path
from typing import Dict, Any, Tuple

from exp_framework.utils.config_utils import deep_merge

DEFAULT_COUNTERS = (
    "anon_fault_alloc", "anon_fault_fallback", "anon_fault_fallback_charge",
    "split", "swpin", "swpout", "zswpout",
)

# ---- 采样配置默认值（sample_config 各域 schema）----

DEFAULT_SAMPLE_CONFIG: dict = {
    "vmstat": {"keys": None, "interval_s": 60,
               "buddyinfo": {"enabled": False, "interval_s": 0}},
    "tasktime": {"procs": [], "interval_s": 2, "strict": True},
    "lock_stat": {"enabled": False},
    "trace": {"captures": [], "strict": True},
    "power": {"odpm": False},
}


def resolve_sample_config(sample_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """默认值 + 用户 sample_config 深合并（数组/标量整体替换）。"""
    return deep_merge(DEFAULT_SAMPLE_CONFIG, dict(sample_cfg or {}))


# ---- 配置加载 / backend 解析 ----

def load_config(path: str) -> Dict[str, Any]:
    """读取输入 config 文件（JSON）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def backend_from_config(config: Dict[str, Any]) -> Tuple[str, dict, dict]:
    """从 config 解析 (backend_name, backend_config, global_config)。

    global_config = config["config"] 去掉 "backend" 后的全局/采样参数。
    """
    cfg = config.get("config", {})
    backend = cfg.get("backend", {})
    name = backend.get("name", "memstress")
    backend_cfg = backend.get("config", {}) or {}
    global_cfg = {k: v for k, v in cfg.items() if k != "backend"}
    return name, backend_cfg, global_cfg


# ---- 运行时清单（run_manifest.json）----

def new_run_manifest(serial: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """由输入 config 生成运行时清单骨架。"""
    return {
        "serial": serial,
        "start_host_ts": int(__import__("time").time()),
        "status": "running",
        "config": config.get("config", {}),
        "sample_config": config.get("sample_config", {}),
    }


def write_run_manifest(manifest: Dict[str, Any], path: Path) -> None:
    """写 run_manifest.json（幂等，多次调用覆盖）。"""
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
