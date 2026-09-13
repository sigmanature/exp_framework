#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""薄壳入口：expf_ui 本地 GUI（Qt）。核心代码在 src/exp_framework/ui/。

用法：
  python3 scripts/expf_ui.py                     # 正常启动
  python3 scripts/expf_ui.py --screenshot p.png  # 无头渲染截图后退出（调试）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exp_framework.ui.main_window import main

if __name__ == "__main__":
    sys.exit(main())
