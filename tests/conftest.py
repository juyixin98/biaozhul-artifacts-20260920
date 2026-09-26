"""pytest 公共夹具。"""

import sys
from pathlib import Path

import pytest

# 让测试无需安装即可导入仓库内的包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def straight_dynamics():
    return {
        "v_max": 1.0,
        "a_max": 0.5,
        "d_max": 0.5,
        "v_start": 0.0,
        "v_end": 0.0,
    }
