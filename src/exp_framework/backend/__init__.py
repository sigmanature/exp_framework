"""实验后端实现包。

import 本包即触发各后端模块的 @register 副作用，将后端类注册进
experiment.REGISTRY。runner 入口应显式 import：
    import experiments  # noqa: F401
"""
from . import memstress  # noqa: F401  (register side effect)
from . import madvise_pagout  # noqa: F401  (register side effect)

__all__ = ["memstress", "madvise_pagout"]
