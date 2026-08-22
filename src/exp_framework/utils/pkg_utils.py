from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence


def read_package_file(path_str: Optional[str]) -> List[str]:
    if not path_str:
        return []
    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(f"package file not found: {path}")
    pkgs: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        x = line.strip()
        if not x or x.startswith("#"):
            continue
        pkgs.append(x)
    return pkgs


def unique_preserve_order(items: Sequence[str]) -> List[str]:
    return list(dict.fromkeys(items))



def verify(config: dict) -> list:
    """包安装校验（gate 预检）：pm list packages 比对。

    config 约定：config["config"]["memstress"]["packages"]，
    config["exp_ctx"] = {"serial"}。
    """
    from . import adb_utils
    ctx = config.get("exp_ctx", {})
    serial = ctx.get("serial")
    pkgs = (config.get("config") or {}).get("memstress", {}).get("packages", [])
    if not pkgs:
        return []
    out = adb_utils.adb_shell(serial, "pm list packages",
                              timeout_s=30, check=False)
    installed = {ln.split(":", 1)[1].strip() for ln in out.splitlines()
                 if ln.startswith("package:")}
    missing = [p for p in pkgs if p not in installed]
    print(f"  {'packages':<28s} = {len(pkgs) - len(missing)}/{len(pkgs)} 已装 "
          f"[{'OK' if not missing else 'MISMATCH(缺 ' + str(missing[:5]) + '...)'}]")
    return [{"param": "packages",
             "expected": f"{len(pkgs)} 个包已安装",
             "actual": f"缺 {len(missing)} 个",
             "ok": not missing}]
