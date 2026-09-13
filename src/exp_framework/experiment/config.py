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
from typing import Dict, Any, List, Optional, Tuple

from exp_framework.utils.config_utils import deep_merge

_DEFAULT_SAMPLE_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "default_sample_config.json")


def load_default_sample_config() -> Dict[str, Any]:
    """读框架默认采样配置模板（default_sample_config.json）。"""
    return json.loads(_DEFAULT_SAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))


def _merge_probe_lists(tpl_probes: List[Dict[str, Any]],
                       cfg_probes: List[Dict[str, Any]],
                       cfg_global_iv: Optional[int] = None) -> List[Dict[str, Any]]:
    """cycle_sample.probes 按 name 合并：配置项与模板同名项深合并，新项追加。

    interval_s 优先级：probe 显式 > cycle_sample.interval_s(全局) > 模板缺省。
    当配置提供了全局 interval 且该 probe 未写 interval_s 时，用全局值
    覆盖模板里的 cadence（"只设全局，其他默认跟全局"）。
    """
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for p in tpl_probes or []:
        if isinstance(p, dict) and p.get("name"):
            by_name.setdefault(str(p["name"]), []).append(p)
    for p in cfg_probes or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        name = str(p["name"])
        merged = dict(p)
        if "interval_s" not in merged and cfg_global_iv:
            merged["interval_s"] = cfg_global_iv
        if name in by_name:
            by_name[name] = [deep_merge(by_name[name][-1], merged)]
        else:
            by_name[name] = [merged]
    out: List[Dict[str, Any]] = []
    for lst in by_name.values():
        out.extend(lst)
    return out


def resolve_sample_config(sample_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """①默认模板 + ②manifest 差异 深合并；probes 按 name 二次合并。"""
    tpl = load_default_sample_config()
    merged = deep_merge(tpl, dict(sample_cfg or {}))
    cfg_cs = dict(sample_cfg or {}).get("cycle_sample") or {}
    tpl_cs = tpl.get("cycle_sample") or {}
    if (isinstance(tpl_cs.get("probes"), list)
            and isinstance(merged.get("cycle_sample", {}).get("probes"), list)
            and isinstance(cfg_cs.get("probes"), list)):
        cfg_global = int(cfg_cs.get("interval_s", 0) or 0) or None
        merged["cycle_sample"]["probes"] = _merge_probe_lists(
            tpl_cs["probes"], cfg_cs["probes"], cfg_global)
    return merged


# ---- 配置加载 / backend 解析 ----

def load_config(path: str) -> Dict[str, Any]:
    """读取输入 config 文件（JSON）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---- 多 round / precondition 编排（框架层，与后端无关）----

DEFAULT_ROUNDS = 1
PRECONDITION_MODES = ("once", "per_round")


def resolve_rounds(global_cfg: Dict[str, Any], cli_value: Any = None) -> int:
    """轮次：config["config"]["rounds"]，CLI 显式传入时以 CLI 为准；未指定默认1。

    rounds=1 保持旧行为（单一实验目录，无 round_* 子目录）。
    rounds < 1 或非整数值 -> ValueError（不静默钳位，防止配置错误被掩盖）。
    """
    if cli_value is not None:
        value = cli_value
    else:
        value = global_cfg.get("rounds", DEFAULT_ROUNDS)
    if value is None or isinstance(value, bool):
        raise ValueError(f"config.config.rounds must be an int >= 1, got {value!r}")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"config.config.rounds must be an int >= 1, got {value!r}")
    try:
        rounds = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"config.config.rounds must be an int >= 1, got {value!r}") from None
    if rounds < 1:
        raise ValueError(f"config.config.rounds must be an int >= 1, got {value!r}")
    return rounds


def resolve_precondition_mode(global_cfg: Dict[str, Any],
                              cli_value: Any = None) -> Optional[str]:
    """precondition 调度模式：config["config"]["precondition"]["mode"]。

    返回 None 表示不启用 precondition（无 precondition 块、mode 为 "none"，
    或 CLI 显式传 none）。块存在且未写 mode 时默认 "once"（所有轮次只打碎一次）。
    想禁用 precondition 时删除整个块即可（mode: null 视为未写）。
    """
    if cli_value is not None:
        mode = str(cli_value)
        return None if mode == "none" else _validate_mode(mode)
    if "precondition" not in global_cfg:
        return None
    block = global_cfg["precondition"]
    if not isinstance(block, dict):
        raise ValueError(f"config.config.precondition must be a dict, got {block!r}")
    mode = block.get("mode")
    if not mode:
        return "once"
    return None if str(mode) == "none" else _validate_mode(str(mode))


def _validate_mode(mode: str) -> str:
    if mode not in PRECONDITION_MODES:
        raise ValueError(
            f"precondition.mode must be one of {PRECONDITION_MODES} "
            f"(or none to disable), got {mode!r}")
    return mode


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
