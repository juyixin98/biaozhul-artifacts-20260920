import sys
from pathlib import Path

# 允许在不安装包的情况下从仓库根目录直接运行 pytest。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
