"""kernel boot 参数：按平台分支处理。

  - pixel：boot 参数由 vendor_boot 的 bootconfig 注入。verify 先回读
    /sys/module/kernel/parameters/<param> 自检；不匹配时重打包 vendor_boot
    （dump 当前镜像 → unpack --format mkbootimg → 更新 bootconfig →
     mkbootimg 重建 → fastboot 刷入 → 重启）再回读。
  - cuttlefish：boot 参数由 CVD 启动命令行注入，verify 检查 /proc/cmdline。

verify(config) 输入约定：
  config["boot_params"] = [{"param", "path"(回读节点), "expected"}]
  config["_ctx"] = {"serial", "platform", "vendor_boot_dir"(可选,默认 ~/learn_os/.worklog/vendor_boot_images)}
每项返回 {"param", "expected", "actual", "ok"} 并当场打印。
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import Any, Dict, List

from exp_framework.utils import adb_utils
from exp_framework.utils import device_nodes

AOSP_HOST_BIN = "/home/nzzhao/learn_os/android17/out/host/linux-x86/bin"
DUMP_PATH = "/data/local/tmp/vb_dump.img"
BLOCK_DEV = "/dev/block/by-name/vendor_boot_b"
_BOOT_DOMAIN_TOKENS = ("内核命令行", "boot", "cmdline")


def verify(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    ctx = config.get("_ctx", {})
    params = config.get("boot_params", [])
    if not params:
        return []
    platform = ctx.get("platform", "pixel")
    if platform == "cuttlefish":
        return _verify_cmdline(ctx.get("serial"), params)
    return _verify_pixel(ctx.get("serial"), params,
                         ctx.get("vendor_boot_dir"))


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


def _verify_pixel(serial: str, params: List[Dict[str, Any]],
                  vendor_boot_dir: str | None) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    need_repackage = False
    for p in params:
        actual = device_nodes.read_node(serial, p["path"])
        ok = actual == str(p["expected"]).strip()
        print(f"  {p['param']:<28s} = {actual!r:<38s} "
              f"[{'OK' if ok else 'MISMATCH(期望=' + p['expected'] + ')'}]")
        results.append({"param": p["param"], "expected": p["expected"],
                        "actual": actual, "ok": ok})
        if not ok:
            need_repackage = True
    if need_repackage:
        if os.environ.get("KERNEL_BOOT_REPACKAGE") != "1":
            print("  boot 参数不匹配，但 KERNEL_BOOT_REPACKAGE != 1（默认安全护栏）："
                  "跳过重打包刷机，请显式设置 KERNEL_BOOT_REPACKAGE=1 再跑")
            return results
        print("  boot 参数不匹配 → 重打包 vendor_boot 并刷入重启")
        img = repackage_vendor_boot(serial, params, vendor_boot_dir)
        flash_and_reboot(serial, img)
        print("  重启完成，回读自检 boot 参数")
        for i, p in enumerate(params):
            actual = device_nodes.read_node(serial, p["path"])
            ok = actual == str(p["expected"]).strip()
            results[i] = {"param": p["param"], "expected": p["expected"],
                          "actual": actual, "ok": ok}
            print(f"  {p['param']:<28s} = {actual!r:<38s} "
                  f"[{'OK' if ok else 'MISMATCH(期望=' + p['expected'] + ')'}]")
    return results


def repackage_vendor_boot(serial: str, params: List[Dict[str, Any]],
                          vendor_boot_dir: str | None = None) -> str:
    """dump 当前 vendor_boot → 更新 bootconfig → mkbootimg 重建 → 返回新 img 绝对路径。"""
    out_dir = vendor_boot_dir or os.path.expanduser(
        "~/learn_os/.worklog/vendor_boot_images")
    os.makedirs(out_dir, exist_ok=True)
    names = "_".join(f"{p['param']}{p['expected']}" for p in params)
    new_img = os.path.join(out_dir, f"vendor_boot_{names}.img")

    with tempfile.TemporaryDirectory() as td:
        adb_utils.adb_shell_root(serial, f"dd if={BLOCK_DEV} of={DUMP_PATH} bs=4k",
                                    timeout_s=120, check=False)
        pulled = os.path.join(td, "vb_dump.img")
        subprocess.run(["adb", "-s", serial, "pull", DUMP_PATH, pulled],
                       capture_output=True, timeout=120, check=True)
        unpack_dir = os.path.join(td, "unpack")
        os.makedirs(unpack_dir, exist_ok=True)
        args_out = os.path.join(td, "mkbootimg_args.txt")
        with open(args_out, "w") as f:
            subprocess.run([os.path.join(AOSP_HOST_BIN, "unpack_bootimg"),
                            "--boot_img", pulled, "--out", unpack_dir,
                            "--format=mkbootimg"],
                           stdout=f, check=True, timeout=60)
        _update_bootconfig(os.path.join(unpack_dir, "bootconfig"), params)
        with open(args_out) as f:
            args = f.read().strip()
        subprocess.run(["sh", "-c",
                        f"{os.path.join(AOSP_HOST_BIN, 'mkbootimg')} {args} "
                        f"--vendor_boot {new_img}"],
                       check=True, timeout=180)
        print(f"  重打包完成: {new_img}")
    return new_img


def _update_bootconfig(bc_path: str, params: List[Dict[str, Any]]) -> None:
    """在 bootconfig 中设置 kernel.<param> = <expected>（已有则替换，无则追加 kernel 块）。"""
    with open(bc_path, encoding="utf-8") as f:
        text = f.read()
    block_match = re.search(r"kernel\s*\{([^}]*)\}", text, re.S)
    lines_extra: List[str] = []
    for p in params:
        key, val = p["param"], str(p["expected"])
        if block_match:
            block = block_match.group(1)
            pat = re.compile(rf"^\s*{re.escape(key)}\s*=\s*[^\n]*$", re.M)
            if pat.search(block):
                block = pat.sub(f"{key} = {val}", block)
            else:
                block = block.rstrip() + f"\n{key} = {val}\n"
            text = (text[:block_match.start()] + "kernel {" + block + "}"
                    + text[block_match.end():])
        else:
            lines_extra.append(f"{key} = {val}")
    if lines_extra:
        text = text.rstrip() + "\nkernel {\n" + "\n".join(lines_extra) + "\n}\n"
    with open(bc_path, "w", encoding="utf-8") as f:
        f.write(text)


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
