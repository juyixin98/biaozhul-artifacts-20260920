"""保证仓库根目录在 sys.path 上（支持从任意目录执行 pytest）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
