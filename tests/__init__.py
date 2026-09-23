"""测试包初始化：把仓库根目录加入 sys.path，便于 `python -m unittest`。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
