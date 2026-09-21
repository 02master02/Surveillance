"""支持 `python -m cuda_monitor` 方式启动。"""

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
