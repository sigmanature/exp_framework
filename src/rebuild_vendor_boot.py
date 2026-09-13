"""rebuild_vendor_boot — 官方 bootarg 表 + 自编译片段组装 vendor_boot（核心实现）。

原理（用户确认的最终形态，无 unpack 设备/改镜像逻辑）：
  vendor_boot = dtb + main ramdisk + dlkm ramdisk + vendor_cmdline + bootconfig
  - vendor_cmdline / bootconfig 官方表：默认仓库 references/vendor_boot_official/，
    可用环境变量 VENDOR_BOOT_OFFICIAL_DIR 覆盖（换 Android build 只换表）
  - dtb / dlkm ramdisk：我们 Kleaf 编译产物——由调用方显式传入（CLI --dtb /
    --ramdisk-dlkm，或 env SELF_BUILT_VENDOR_BOOT_DIST 指定 dist 目录）
  - main ramdisk：设备 dump（含 magisk），留档目录 env VENDOR_BOOT_ARCHIVE_DIR
    （默认 ~/.worklog/vendor_boot_images）
  - bootconfig = 官方键 + kernel{注入参数...}

路径不耦合：本模块不硬编码任何机器路径——工具、官方表、自编译片段、留档目录
全部经参数 / 环境变量注入，缺省仅回退到"约定目录"（~/.worklog、仓库 references）。

本模块同时是 kernel_boot_utils boot_params 校验链的重打包后端：
boot 参数不匹配 → repackage_boot_params() 用官方表 + 自编译片段重组 → flash。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]   # exp_framework 仓库根
VENDOR_BOOT_B = "/dev/block/by-name/vendor_boot_b"
DUMP_PATH = "/data/local/tmp/vb_dump.img"
DEFAULT_ARCHIVE_DIR = Path.home() / ".worklog" / "vendor_boot_images"
DEFAULT_RAMDISK_MAIN_NAME = "ramdisk00_magisk.img"

# mkbootimg 结构参数（照抄 cp2a 官方 vendor_boot 组装命令，保持一致）
MKBOOTIMG_BASE_ARGS = [
    "--header_version", "4",
    "--pagesize", "0x00000800",
    "--base", "0x00000000",
    "--kernel_offset", "0x10008000",
    "--ramdisk_offset", "0x11000000",
    "--tags_offset", "0x10000100",
    "--dtb_offset", "0x0000000011f00000",
]


def _host_tool(name: str) -> str:
    """mkbootimg/unpack_bootimg 定位：ANDROID_HOST_BIN 目录 → PATH。"""
    env_dir = os.environ.get("ANDROID_HOST_BIN")
    if env_dir:
        cand = Path(env_dir) / name
        if cand.exists():
            return str(cand)
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(
        f"找不到 {name}：设置 ANDROID_HOST_BIN 目录或加入 PATH")


def official_dir() -> Path:
    """官方 bootarg 表目录（换 Android build 时整体替换该目录内容）。"""
    d = os.environ.get("VENDOR_BOOT_OFFICIAL_DIR")
    return Path(d) if d else REPO_ROOT / "references" / "vendor_boot_official"


def official_table(name: str) -> Path:
    return official_dir() / name


def archive_dir() -> Path:
    """留档目录（ramdisk-main dump 等）：env 优先，默认 ~/.worklog。"""
    d = os.environ.get("VENDOR_BOOT_ARCHIVE_DIR")
    return Path(d) if d else DEFAULT_ARCHIVE_DIR


def ramdisk_main_archive() -> Path:
    return archive_dir() / DEFAULT_RAMDISK_MAIN_NAME


def _read_table(path: Path) -> str:
    """读官方表文件（去掉 # 注释行）。"""
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    return "\n".join(lines)


def build_bootconfig_text(params: Sequence[Dict[str, str]]) -> str:
    """官方 bootconfig 键 + kernel{param=value...}。"""
    text = _read_table(official_table("bootconfig.txt")) + "\n"
    if params:
        body = "\n".join(f"{p['param']} = {p['expected']}" for p in params)
        text += f"kernel {{\n{body}\n}}\n"
    return text


def assemble(dtb: Path, ramdisk_main: Path, ramdisk_dlkm: Path,
             params: Sequence[Dict[str, str]], out: Path) -> Path:
    """官方表 + 三个输入片段 → mkbootimg 组装。"""
    cmdline = _read_table(official_table("vendor_cmdline.txt")).strip()
    with tempfile.TemporaryDirectory() as td:
        bc = Path(td) / "bootconfig"
        bc.write_text(build_bootconfig_text(params), encoding="utf-8")
        cmd = ([_host_tool("mkbootimg")] + MKBOOTIMG_BASE_ARGS
               + ["--vendor_cmdline", cmdline,
                  "--board", "",
                  "--dtb", str(dtb),
                  "--vendor_bootconfig", str(bc),
                  "--ramdisk_type", "1", "--ramdisk_name", "",
                  "--vendor_ramdisk_fragment", str(ramdisk_main),
                  "--ramdisk_type", "3", "--ramdisk_name", "dlkm",
                  "--vendor_ramdisk_fragment", str(ramdisk_dlkm),
                  "--vendor_boot", str(out)])
        subprocess.run(cmd, check=True, timeout=180)
    print(f"  vendor_boot 组装完成（官方表+自编译片段）: {out}", flush=True)
    return out


def dump_ramdisk_main(serial: str, out: Path) -> Path:
    """dd 设备 vendor_boot 槽 → 取 main ramdisk（含 magisk）→ 留档 out。

    只需 dump 一次；之后 assemble 永久复用该留档。
    """
    subprocess.run(["adb", "-s", serial, "shell",
                    f"dd if={VENDOR_BOOT_B} of={DUMP_PATH} bs=4k"],
                   capture_output=True, timeout=120, check=False)
    with tempfile.TemporaryDirectory() as td:
        pulled = Path(td) / "vb.img"
        subprocess.run(["adb", "-s", serial, "pull", DUMP_PATH, str(pulled)],
                       capture_output=True, timeout=120, check=True)
        unpack = Path(td) / "unpack"
        unpack.mkdir()
        subprocess.run([_host_tool("unpack_bootimg"),
                        "--boot_img", str(pulled), "--out", str(unpack),
                        "--format=mkbootimg"],
                       capture_output=True, timeout=60, check=True)
        src = unpack / "vendor_ramdisk00"
        if not src.exists():
            raise RuntimeError("dump 镜像里没有 vendor_ramdisk00（main fragment）")
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", str(src), str(out)], check=True)
    print(f"  ramdisk-main 留档: {out} ({out.stat().st_size} B)", flush=True)
    return out


def ensure_ramdisk_main(serial: str, archive: Optional[Path] = None) -> Path:
    """ramdisk-main 留档存在则复用，否则从设备 dump 一次。"""
    target = archive or ramdisk_main_archive()
    if target.exists():
        return target
    return dump_ramdisk_main(serial, target)


def repackage_boot_params(serial: str,
                          params: Sequence[Dict[str, str]],
                          out_img: Path,
                          dist_dir: Optional[Path] = None,
                          archive: Optional[Path] = None) -> Path:
    """框架 boot_params 校验链路后端：官方表 + 自编译片段组装新 vendor_boot。

    params: [{"param", "expected"}, ...]（bootconfig kernel 块注入）。
    dist_dir：自编译 Kleaf 产物目录（dtb.img / initramfs.img），缺省取 env
    SELF_BUILT_VENDOR_BOOT_DIST，再缺省报错（不硬编码路径）。
    archive：ramdisk-main 留档文件/目录，缺省取 env VENDOR_BOOT_ARCHIVE_DIR
    或 ~/.worklog/vendor_boot_images。
    """
    if dist_dir is None:
        env_dist = os.environ.get("SELF_BUILT_VENDOR_BOOT_DIST")
        if not env_dist:
            raise RuntimeError(
                "需要自编译片段目录：传 dist_dir 或设 "
                "SELF_BUILT_VENDOR_BOOT_DIST（含 dtb.img + initramfs.img）")
        dist_dir = Path(env_dist)
    dtb = dist_dir / "dtb.img"
    dlkm = dist_dir / "initramfs.img"
    if not dtb.exists() or not dlkm.exists():
        raise RuntimeError(f"自编译片段缺失: {dist_dir} 需要 dtb.img + initramfs.img")
    main = ensure_ramdisk_main(serial, archive=archive)
    return assemble(dtb, main, dlkm, params, out_img)


def main(argv: Sequence[str] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="官方 bootarg 表 + 自编译片段组装 vendor_boot")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("assemble")
    a.add_argument("--dtb", required=True)
    a.add_argument("--ramdisk-main", required=True)
    a.add_argument("--ramdisk-dlkm", required=True)
    a.add_argument("--param", action="append", default=[], metavar="K=V",
                   help="bootconfig kernel 注入参数，可多次（如 fs_disable_large_folio=0）")
    a.add_argument("--out", required=True)

    d = sub.add_parser("dump-ramdisk-main")
    d.add_argument("--serial", required=True)
    d.add_argument("--out", required=True)

    args = p.parse_args(argv)
    if args.cmd == "assemble":
        params = []
        for kv in args.param:
            k, _, v = kv.partition("=")
            params.append({"param": k, "expected": v})
        assemble(Path(args.dtb), Path(args.ramdisk_main), Path(args.ramdisk_dlkm),
                 params, Path(args.out))
    else:
        dump_ramdisk_main(args.serial, Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
