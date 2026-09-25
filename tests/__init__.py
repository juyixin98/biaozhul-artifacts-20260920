"""测试包初始化：把仓库根目录加入 sys.path，使 ``import packing`` 可用。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
