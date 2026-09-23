"""pytest 根配置：保证从任意目录运行 pytest 都能 import app。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
