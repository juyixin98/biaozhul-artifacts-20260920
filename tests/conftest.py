"""pytest 公共夹具与标记注册。"""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "unit: 单元测试")
    config.addinivalue_line("markers", "integration: 端到端集成测试")
