"""pytest 共享设置: 确保从仓库根目录可导入 eigenservice。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
