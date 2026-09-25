"""把 src/ 加入 sys.path(本项目零依赖,无需安装即可运行测试)。"""

import sys
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
