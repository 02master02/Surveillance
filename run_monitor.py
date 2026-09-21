#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动器。

放在项目根目录，作用是把 src/ 加进 sys.path，让任务计划程序
可以用一句 `pythonw.exe run_monitor.py --config <路径>` 把服务拉起来，
而且不依赖当前工作目录（sys.path[0] 始终是脚本所在目录）。

这个文件刻意不放进包内，保持"一个入口 + 一个 src 包"的简单结构。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from cuda_monitor.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
