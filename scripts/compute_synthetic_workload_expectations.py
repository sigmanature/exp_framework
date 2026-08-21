#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""薄壳入口：核心代码位于 src/exp_framework/compute_synthetic_workload_expectations.py（src-layout 标准组织）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exp_framework.compute_synthetic_workload_expectations import main

if __name__ == "__main__":
    sys.exit(main())
