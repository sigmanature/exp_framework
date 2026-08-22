"""配置加载与解析（输入 config -> 后端参数 + 采样配置），运行时清单写入。

术语：
  config          输入配置（用户修改的参数），见 config/*.json
  run_manifest.json  运行时清单（框架生成的结果档案，输出到实验目录）

组装模型（Kustomize 式分层合成）：
  ① 框架默认层  config/default_sample_config.json（采样默认值，含 cycle_sample 各域）
  ② 实验差异层  manifest 的 sample_config（只写差异，deep_merge 合成）
  ③ CLI 覆盖    现有 argparse 覆盖（config 层，见 runner._global_overrides）
  ④ 运行时清单  run_manifest.json（产物：合成后完整配置）

config 顶层结构：
{
  "serial": "21121FDF600C4G",
  "config": {
    "counters": [...],
    "interval_s": 60,
    "backend": {"name": "memstress", "config": { ...后端私有参数... }}
  },
  "sample_config": {
    "cycle_sample": {
      "counters":  {...}, "vmstat": {...}, "buddyinfo": {...},
      "thermal":   {...}, "cpufreq": {...}
    },
    "tasktime": {...}, "trace": {...}, "lock_stat": {...}, "power": {...}
  }
}

cycle_sample = 周期采样统一抽象：任何按固定间隔周期采集的样本都是一个子域，
每个子域独立 enabled/interval_s/字段/输出文件，见 utils/cycle_sample.py。
"""
import json
from pathlib import Path
from typing import Dict, Any, Tuple

from exp_framework.utils.config_utils import deep_merge

_DEFAULT_SAMPLE_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "default_sample_config.json")


def load_default_sample_config() -> Dict[str, Any]:
    """读框架默认采样配置模板（default_sample_config.json）。"""
    return json.loads(_DEFAULT_SAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))


def resolve_sample_config(sample_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """①默认模板 + ②manifest 差异 深合并（数组/标量整体替换）。"""
    return deep_merge(load_default_sample_config(), dict(sample_cfg or {}))


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
