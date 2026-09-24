"""pytest 配置：把仓库根目录加入 sys.path，使 ``import speed_profile`` 可用。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
