"""pytest 全局配置：把 src/ 加入导入路径（无需安装包）。"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
