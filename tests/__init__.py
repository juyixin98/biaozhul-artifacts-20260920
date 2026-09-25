"""测试包初始化：把仓库根目录加入 sys.path，便于直接 `python -m unittest`。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
