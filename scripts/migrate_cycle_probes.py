#!/usr/bin/env python3
"""一次性迁移：config/*.json 的 cycle_sample 旧形态 -> 最终 probe 形态。

v1 形态（上一版）：{"interval_s": 全局, "probes":[{..., every_n:N}]}
最终形态：         {"interval_s": 全局缺省, "probes":[{..., interval_s}]}

规则：
  - probe 有 every_n → interval_s = 全局 interval_s × every_n，删 every_n；
  - parse "counters" → "kv"，keys 补 order_0..15（folio_alloc 的 kv 列），
    派生 totals 不再由通用采样器计算；
  - 旧域 dict 形态（counters/vmstat/buddyinfo/thermal/cpufreq）直接转 probe；
  - 只有全局 interval、无 probe 的配置：保留 interval_s（作为模板缺省）。

用法: python3 scripts/migrate_cycle_probes.py [--dry]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
sys.path.insert(0, str(ROOT / "src"))

from exp_framework.experiment.config import resolve_sample_config  # noqa: E402
from exp_framework.utils.cycle_sample import norm_probe  # noqa: E402

THERMAL_TPL = "/sys/class/thermal/{key}/temp"
CPUFREQ_TPL = "/sys/devices/system/cpu/cpu{key}/cpufreq/scaling_cur_freq"
STATS_DIR = "/data/local/tmp/memstress_stats"
FOLIO_EXTRA = "/sys/kernel/mm/readahead/folio_alloc"
ORDER_COLS = [f"order_{i}" for i in range(16)]


def _fix_probe(pr: dict, global_iv: int) -> dict:
    """every_n→interval_s；counters/int→kv；空 keys/cols 让模板补齐。"""
    if "every_n" in pr:
        n = max(1, int(pr.pop("every_n", 1) or 1))
        g = global_iv if global_iv > 0 else 60
        pr["interval_s"] = max(1, g * n)
    if pr.get("parse") == "counters":
        pr["parse"] = "kv"
        ks = list(pr.get("keys") or [])
        if not any(k.startswith("order_") for k in ks):
            pr["keys"] = ks + ORDER_COLS
    elif pr.get("parse") == "int":
        pr["parse"] = "kv"
        if not (pr.get("keys") or pr.get("cols")):
            pr.pop("keys", None)
            pr.pop("cols", None)
    elif pr.get("parse") in ("buddyinfo", "pagetypeinfo"):
        orig = pr["parse"]
        pr["parse"] = "rows"
        pr["label_keys"] = (["zone"] if orig == "buddyinfo"
                            else ["zone", "type"])
    return pr


def migrate_cs(cs: dict) -> dict:
    old = {k: v for k, v in cs.items()
           if isinstance(v, dict) and k in
           ("counters", "vmstat", "buddyinfo", "thermal", "cpufreq")}
    global_iv = int(cs.get("interval_s", 0) or 60) or 60

    if old:
        probes = []

        def en(cfg: dict) -> bool:
            return bool(cfg.get("enabled", True)) \
                and int(cfg.get("interval_s", 0)) > 0

        def iv(cfg: dict) -> int:
            return max(1, int(cfg.get("interval_s", global_iv)))

        if en(old.get("counters", {})):
            probes.append({"name": "counters", "kind": "dir",
                           "path": STATS_DIR,
                           "keys": list(old["counters"].get("keys", []))
                                   + ORDER_COLS,
                           "parse": "kv", "extra_paths": [FOLIO_EXTRA],
                           "out": "raw_samples.csv", "enabled": True,
                           "interval_s": iv(old["counters"])})
        if en(old.get("vmstat", {})):
            v = {"name": "vmstat", "kind": "file", "path": "/proc/vmstat",
                 "parse": "kv", "out": "vmstat_samples.csv", "enabled": True,
                 "interval_s": iv(old["vmstat"])}
            if old["vmstat"].get("keys"):
                v["keys"] = list(old["vmstat"]["keys"])
            probes.append(v)
        if en(old.get("buddyinfo", {})):
            probes.append({"name": "buddyinfo", "kind": "file",
                           "path": "/proc/buddyinfo", "parse": "rows",
                           "label_keys": ["zone"],
                           "out": "buddyinfo_samples.csv", "enabled": True,
                           "interval_s": iv(old["buddyinfo"])})
        if en(old.get("thermal", {})):
            zones = list(old["thermal"].get("zones", []))
            probes.append({"name": "thermal", "kind": "files",
                           "path_template": THERMAL_TPL, "keys": zones,
                           "cols": [f"temp_{z.split('_')[-1]}" for z in zones],
                           "parse": "kv", "out": "thermal_samples.csv",
                           "enabled": True, "interval_s": iv(old["thermal"])})
        if en(old.get("cpufreq", {})):
            cpus = list(old["cpufreq"].get("cpus", []))
            probes.append({"name": "cpufreq", "kind": "files",
                           "path_template": CPUFREQ_TPL, "keys": cpus,
                           "cols": [f"freq_cpu{c}" for c in cpus],
                           "parse": "kv", "out": "cpufreq_samples.csv",
                           "enabled": True,
                           "interval_s": iv(old["cpufreq"])})
        return {"interval_s": global_iv, "probes": probes}

    probes = []
    for pr in cs.get("probes", []) or []:
        if not isinstance(pr, dict):
            continue
        probes.append(_fix_probe(dict(pr), global_iv))
    return {"interval_s": global_iv, "probes": probes}


def migrate_file(path: Path, dry: bool) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    sc = data.get("sample_config") or {}
    if "cycle_sample" not in sc:
        return False
    new_cs = migrate_cs(sc["cycle_sample"])
    if new_cs == sc["cycle_sample"]:
        return False
    data["sample_config"]["cycle_sample"] = new_cs
    if not dry:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    return True


def verify(path: Path) -> bool:
    """合并后校验：resolve_sample_config 后每个 probe norm_probe 合规。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    merged = resolve_sample_config(data.get("sample_config") or {})
    cs = merged.get("cycle_sample") or {}
    probes = cs.get("probes") or []
    if not probes:
        return True
    for p in probes:
        if p.get("enabled", False):
            try:
                norm_probe(p)
            except ValueError:
                return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    changed = verified = 0
    for path in sorted(CONFIG_DIR.glob("*.json")):
        if path.name == "default_sample_config.json":
            continue
        changed += migrate_file(path, a.dry)
        if verify(path):
            verified += 1
        else:
            print(f"VERIFY FAIL {path.name}", file=sys.stderr)
    print(f"migrated={changed} verified={verified}"
          f"{' (dry run)' if a.dry else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())