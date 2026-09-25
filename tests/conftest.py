"""pytest 共享路径配置：无需安装包即可导入 src/blp。"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)
