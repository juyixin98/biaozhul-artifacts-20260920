"""让 tests/ 目录下的测试在未安装包时也能找到项目根目录的 adaptive_integration。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
