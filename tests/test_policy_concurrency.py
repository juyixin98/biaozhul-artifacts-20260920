"""验收场景五：策略更新竞争与版本固定。

* publish 产生不可变新版本，旧版本仍可读取与复算；
* expected_version 乐观锁阻止丢失更新（第二个提交得到 409/Conflict）；
* 导出任务固定到解析时的具体版本，之后发布新版本不影响在途/已完成导出；
* 决策记录与清单引用策略 id/version/fingerprint，可据此取回快照复算。
"""

import json

import pytest

from mde.errors import PolicyConflict, PolicyNotFound
from mde.policy import FilePolicyStore, InMemoryPolicyStore
from mde.service import ExportService
from mde.engine import value_hash
from mde.models import Rule


def _rules_v1():
    return [
        {"path": "name", "action": "allow"},
        {"path": "email", "action": "generalize", "generalizer": "email_mask"},
    ]


def test_publish_creates_immutable_versions(store):
    p1 = store.publish("p", _rules_v1())
    p2 = store.publish("p", [{"path": "name", "action": "allow"},
                             {"path": "email", "action": "deny"}])
    assert (p1.version, p2.version) == (1, 2)
    assert store.get("p", 1).fingerprint == p1.fingerprint
    assert store.get("p", 1) is not store.get("p", 2)
    # v1 内容保持不变。
    assert any(r.path == "email" and r.action == "generalize"
               for r in store.get("p", 1).rules)
    assert store.list_versions("p") == [1, 2]


def test_optimistic_concurrency_rejects_stale_update(store):
    store.publish("p", _rules_v1())  # v1
    # 两个起草者都基于 v1。
    store.publish("p", [{"path": "name", "action": "allow"}],
                  expected_version=1)  # 第一人成功 -> v2
    with pytest.raises(PolicyConflict) as ei:
        store.publish("p", [{"path": "name", "action": "deny"}],
                      expected_version=1)  # 第二人的 v1 已过期
    assert ei.value.current == 2
    # 第二人重新读取合并后基于 v2 提交 -> v3 成功。
    p3 = store.publish("p", [{"path": "name", "action": "deny"}],
                       expected_version=2)
    assert p3.version == 3


def test_export_pins_resolved_version_and_snapshot(service, store):
    store.publish("p", _rules_v1())  # v1：email 掩码
    data = {"name": "A", "email": "a@x.com", "extra": 1}

    # 导出固定到 v1（即便随后策略更新）。
    bundle_v1 = service.export(data, "p", "analytics")
    assert bundle_v1["manifest"]["policy"]["version"] == 1

    store.publish("p", [{"path": "name", "action": "allow"},
                        {"path": "email", "action": "deny"}])  # v2

    # 新导出默认跟随最新 v2；但旧导出包内快照仍是 v1，复算一致。
    bundle_v2 = service.export(data, "p", "analytics")
    assert bundle_v2["manifest"]["policy"]["version"] == 2
    assert bundle_v1["output"]["email"] == "a***@x.com"
    assert "email" not in bundle_v2["output"]

    # 用 v1 包内嵌的快照重算，结果与 v1 包完全一致（不受存储当前是 v2 影响）。
    report = service.verify_bundle(bundle_v1, source_data=data)
    assert report["policy"]["version"] == 1
    assert report["ok"] is True

    # 显式固定版本也可用：即使当前是 v2，仍能按 v1 导出。
    pinned = service.export(data, "p", "analytics", version=1)
    assert pinned["output"]["email"] == "a***@x.com"
    assert pinned["manifest"]["policy"]["version"] == 1


def test_pinned_nonexistent_version_raises(service, store):
    store.publish("p", _rules_v1())
    with pytest.raises(PolicyNotFound):
        service.export({"name": "A"}, "p", "analytics", version=99)


def test_concurrent_publish_threads(store):
    """多线程同时发布：版本号不重复、不跳号。"""
    import threading

    errors = []

    def pub(i):
        try:
            store.publish("p", [{"path": f"f{i}", "action": "allow"}])
        except PolicyConflict:
            # 不带 expected_version 的发布不应冲突；记录为异常。
            errors.append("unexpected conflict")

    threads = [threading.Thread(target=pub, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert store.list_versions("p") == list(range(1, 21))


def test_file_store_roundtrip_and_immutability(tmp_path):
    d = str(tmp_path / "policies")
    s1 = FilePolicyStore(d)
    p1 = s1.publish("p", _rules_v1())
    s1.publish("p", [{"path": "name", "action": "deny"}])

    s2 = FilePolicyStore(d)  # 新实例从磁盘恢复
    assert s2.list_versions("p") == [1, 2]
    assert s2.get("p", 1).fingerprint == p1.fingerprint
    with pytest.raises(PolicyConflict):
        # 文件存储不允许覆盖已存在版本（通过 load_dict 路径验证不变性）。
        s2.load_dict(s2.get("p", 1).to_dict())
