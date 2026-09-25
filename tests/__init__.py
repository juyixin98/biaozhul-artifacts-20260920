# 让测试无需安装即可直接从仓库根目录导入
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
