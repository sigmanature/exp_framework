#!/usr/bin/env python3
"""薄壳入口：官方 bootarg 表 + 自编译片段组装 vendor_boot。

实际逻辑在 src/exp_framework/rebuild_vendor_boot.py。
用法示例：
  python3 scripts/rebuild_vendor_boot.py dump-ramdisk-main --serial S --out <留档.img>
  python3 scripts/rebuild_vendor_boot.py assemble --dtb dist/dtb.img \
      --ramdisk-main <留档> --ramdisk-dlkm dist/initramfs.img \
      --param fs_disable_large_folio=0 --out vendor_boot_folio0.img
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from exp_framework.rebuild_vendor_boot import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
