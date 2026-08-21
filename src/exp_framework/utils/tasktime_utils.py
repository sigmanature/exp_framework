"""tasktime 目标线程名校验：复用 run_memstress 的 resolve_tasktime_targets（pgrep -x）。

verify(config) 输入约定：
  config["sample_config"]["tasktime"]["procs"] = ["kswapd0", ...]
  config["_ctx"] = {"serial"}
"""
from __future__ import annotations

from typing import Any, Dict, List

from exp_framework.experiment.sample import resolve_tasktime_targets


def verify(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    ctx = config.get("_ctx", {})
    serial = ctx.get("serial")
    procs = (config.get("sample_config") or {}).get("tasktime", {}).get("procs", [])
    if not procs:
        return []
    targets = resolve_tasktime_targets(
        serial, procs, strict=False)
    missing = [t.name for t in targets if t.pid is None]
    actual = ", ".join(f"{t.name}={t.pid}" for t in targets)
    print(f"  {'tasktime_procs':<28s} = {actual:<38s} "
          f"[{'OK' if not missing else 'MISMATCH(缺 ' + str(missing) + ')'}]")
    return [{"param": "tasktime_procs", "expected": str(list(procs)),
             "actual": actual, "ok": not missing}]
