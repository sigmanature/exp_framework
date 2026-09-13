"""kernel boot 参数：按平台分支处理。

  - pixel：boot 参数经 vendor_boot 的 bootconfig/vendor_cmdline 注入。verify
    先回读 /sys/module/kernel/parameters/<param> 自检；不匹配时委托
    rebuild_vendor_boot.repackage_boot_params——官方 bootarg 表
    (references/vendor_boot_official/*.txt) + 自编译片段（dist/dtb.img、
    dist/initramfs.img）+ 设备 dump 的 main ramdisk（含 magisk）→
    mkbootimg 组装 → fastboot 刷入 → 重启 → 回读。
  - cuttlefish：boot 参数由 CVD 启动命令行注入，verify 检查 /proc/cmdline。

verify(config) 输入约定：
  config["boot_params"] = [{"param", "path"(回读节点), "expected"}]
  config["exp_ctx"] = {"serial", "platform", "vendor_boot_dir"(可选,默认 ~/learn_os/.worklog/vendor_boot_images)}
每项返回 {"param", "expected", "actual", "ok"} 并当场打印。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from exp_framework.utils import adb_utils
from exp_framework.utils import device_nodes

_BOOT_DOMAIN_TOKENS = ("内核命令行", "boot", "cmdline")


def verify(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    ctx = config.get("exp_ctx", {})
    params = config.get("boot_params", [])
    if not params:
        return []
    platform = ctx.get("platform", "pixel")
    if platform == "cuttlefish":
        return _verify_cmdline(ctx.get("serial"), params)
    return _verify_pixel(ctx.get("serial"), params,
                         ctx.get("vendor_boot_dir"),
                         ctx.get("vendor_boot_dist_dir"))


def _verify_cmdline(serial: str,
                        params: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = adb_utils.adb_shell(serial, "cat /proc/cmdline",
                              timeout_s=15, check=False)
    results: List[Dict[str, Any]] = []
    for p in params:
        token = f"{p['param']}={p['expected']}"
        ok = token in out
        print(f"  {p['param']:<28s} = cmdline{'+' if ok else '-'}{token} "
              f"[{'OK' if ok else 'MISMATCH'}]")
        results.append({"param": p["param"], "expected": p["expected"],
                        "actual": token if ok else "(cmdline 未含)", "ok": ok})
    return results


def _match_param(actual: str, p: Dict[str, Any]) -> bool:
    """boot 参数校验：默认严格相等；contains=True 时做子串匹配
    （用于 /proc/cmdline 这类整行内容，例如 thp_anon=16K:always）。"""
    if p.get("contains"):
        return str(p["expected"]) in actual
    return actual == str(p["expected"]).strip()


def _verify_pixel(serial: str, params: List[Dict[str, Any]],
                  vendor_boot_dir: str | None,
                  dist_dir: str | None = None) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    need_repackage = False
    for p in params:
        actual = device_nodes.read_node(serial, p["path"])
        ok = _match_param(actual, p)
        print(f"  {p['param']:<28s} = {actual!r:<38s} "
              f"[{'OK' if ok else 'MISMATCH(期望=' + p['expected'] + ')'}]")
        results.append({"param": p["param"], "expected": p["expected"],
                        "actual": actual, "ok": ok})
        if not ok:
            need_repackage = True
    if need_repackage:
        print("  boot 参数不匹配 → 重打包 vendor_boot 并刷入重启")
        img = repackage_vendor_boot(serial, params, vendor_boot_dir,
                                    dist_dir=dist_dir)
        flash_and_reboot(serial, img)
        print("  重启完成，回读自检 boot 参数")
        for i, p in enumerate(params):
            actual = device_nodes.read_node(serial, p["path"])
            ok = _match_param(actual, p)
            results[i] = {"param": p["param"], "expected": p["expected"],
                          "actual": actual, "ok": ok}
            print(f"  {p['param']:<28s} = {actual!r:<38s} "
                  f"[{'OK' if ok else 'MISMATCH(期望=' + p['expected'] + ')'}]")
    return results


def repackage_vendor_boot(serial: str, params: List[Dict[str, Any]],
                          vendor_boot_dir: str | None = None,
                          dist_dir: str | None = None,
                          archive_dir: str | None = None) -> str:
    """boot 参数不匹配时的修复链：官方表 + 自编译片段组装新 vendor_boot。

    委托 exp_framework.rebuild_vendor_boot.repackage_boot_params：
      bootconfig = 官方键 + kernel{param=expected...}
      vendor_cmdline = 官方表原样（disable_dma32=on 等）
      dtb / dlkm ramdisk = 自编译 Kleaf 产物（dist_dir，或 env
        SELF_BUILT_VENDOR_BOOT_DIST；不硬编码路径）
      main ramdisk = 设备 dump 留档（archive_dir，或 env
        VENDOR_BOOT_ARCHIVE_DIR / ~/.worklog/vendor_boot_images）
    返回新 img 绝对路径。
    """
    from exp_framework.rebuild_vendor_boot import (archive_dir as default_archive,
                                                   ramdisk_main_archive)
    out_dir = Path(vendor_boot_dir) if vendor_boot_dir else default_archive()
    out_dir.mkdir(parents=True, exist_ok=True)
    names = "_".join(f"{p['param']}{p['expected']}" for p in params)
    new_img = out_dir / f"vendor_boot_my_{names}.img"

    archive_file = (Path(archive_dir) if archive_dir
                    else ramdisk_main_archive())
    from exp_framework.rebuild_vendor_boot import repackage_boot_params
    repackage_boot_params(serial, list(params), new_img,
                          dist_dir=Path(dist_dir) if dist_dir else None,
                          archive=archive_file)
    return str(new_img)


def flash_and_reboot(serial: str, img: str) -> None:
    """重启进 bootloader → fastboot flash vendor_boot → 重启 → 等 boot 完成（launch 流程搬运）。"""
    subprocess.run(["adb", "-s", serial, "reboot", "bootloader"],
                   timeout=30, check=False)
    i = 0
    while i < 60:
        if subprocess.run(["fastboot", "devices"], capture_output=True,
                          text=True, timeout=15).stdout.find(serial) >= 0:
            break
        subprocess.run(["sleep", "1"], check=False)
        i += 1
    subprocess.run(["fastboot", "flash", "vendor_boot", img],
                   timeout=300, check=True)
    subprocess.run(["fastboot", "reboot"], timeout=30, check=False)
    subprocess.run(["adb", "wait-for-device"], timeout=600, check=False)
    i = 0
    while i < 300:
        booted = adb_utils.adb_shell(serial, "getprop sys.boot_completed",
                                     timeout_s=15, check=False).strip()
        if booted == "1":
            break
        subprocess.run(["sleep", "2"], check=False)
        i += 2
    subprocess.run(["sleep", "10"], check=False)
