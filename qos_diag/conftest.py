"""pytest 引导：把项目根加入 sys.path，并在有 ROS 时 source 提示。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
