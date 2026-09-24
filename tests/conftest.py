"""pytest 共享夹具：在临时数据库上构建 TestClient，并开启测试缝隙。

测试缝隙（test seam）
--------------------
为了确定性地验收"规划期间地图变化须重新校验"，这里把 service.plan_single /
plan_batch 包了一层：客户端可以在环境无关的前提下，通过 app.state 注入一个
``before_search_hook(snapshot)``，它在快照读取之后、CPU 搜索之前执行。测试中用它
在另一个数据库连接上提交地图变更，从而让提交阶段的版本复核必然失败（412）。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_tmpdir = tempfile.mkdtemp(prefix="spatio_test_")
_db_path = os.path.join(_tmpdir, "test.db")
os.environ["SPATIO_DB"] = _db_path

from fastapi.testclient import TestClient  # noqa: E402

from app import main, service  # noqa: E402

_orig_plan_single = service.plan_single
_orig_plan_batch = service.plan_batch


@pytest.fixture
def client():
    # 每个用例都重置回默认场景。
    with TestClient(main.app) as c:
        c.post("/admin/reset")
        main.app.state.before_search_hook = None

        def patched_single(conn_factory, **kwargs):
            hook = getattr(main.app.state, "before_search_hook", None)
            if hook is not None:
                kwargs["before_search_hook"] = hook
            return _orig_plan_single(conn_factory, **kwargs)

        def patched_batch(conn_factory, **kwargs):
            hook = getattr(main.app.state, "before_search_hook", None)
            if hook is not None:
                kwargs["before_search_hook"] = hook
            return _orig_plan_batch(conn_factory, **kwargs)

        main.service.plan_single = patched_single  # main 模块里引用的是 service 函数
        main.service.plan_batch = patched_batch
        try:
            yield c
        finally:
            main.app.state.before_search_hook = None
            main.service.plan_single = _orig_plan_single
            main.service.plan_batch = _orig_plan_batch


@pytest.fixture
def db_path() -> Path:
    return Path(_db_path)
