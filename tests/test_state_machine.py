"""状态机迁移测试：生成、激活、停用、销毁及全部非法迁移。"""

import pytest

from keyvault import (
    InvalidStateTransitionError,
    KeyNotFoundError,
    KeyService,
    KeyState,
)


def test_generate_creates_generated_state(svc):
    meta = svc.generate_key()
    assert meta.version_id == "v1"
    assert meta.state == KeyState.GENERATED
    assert svc.status()["active_version"] is None


def test_full_lifecycle(svc):
    svc.generate_key()
    assert svc.activate("v1").state == KeyState.ACTIVE
    assert svc.status()["active_version"] == "v1"
    assert svc.deactivate("v1").state == KeyState.DEACTIVATED
    assert svc.status()["active_version"] is None
    assert svc.destroy("v1").state == KeyState.DESTROYED


def test_activate_supersedes_previous(svc):
    svc.generate_key()
    svc.generate_key()
    svc.activate("v1")
    svc.activate("v2")
    metas = {m["version_id"]: m["state"] for m in svc.list_keys()}
    assert metas == {"v1": "DEACTIVATED", "v2": "ACTIVE"}
    assert svc.status()["active_version"] == "v2"


def test_reactivate_deactivated_key(svc):
    svc.generate_key()
    svc.activate("v1")
    svc.deactivate("v1")
    assert svc.activate("v1").state == KeyState.ACTIVE


def test_destroy_generated_key_allowed(svc):
    svc.generate_key()
    assert svc.destroy("v1").state == KeyState.DESTROYED


@pytest.mark.parametrize(
    "ops, forbidden",
    [
        # (前置操作, 不允许的迁移)
        ([], "deactivate"),                       # GENERATED 不能直接停用
        (["activate"], "destroy"),                # ACTIVE 必须先停用再销毁
        (["activate"], "activate"),               # 重复激活
        (["activate", "deactivate", "destroy"], "activate"),    # 销毁后不可激活
        (["activate", "deactivate", "destroy"], "deactivate"),  # 销毁后不可停用
        (["activate", "deactivate", "destroy"], "destroy"),     # 重复销毁
    ],
)
def test_invalid_transitions_rejected(svc, ops, forbidden):
    svc.generate_key()
    for op in ops:
        getattr(svc, op)("v1")
    with pytest.raises(InvalidStateTransitionError):
        getattr(svc, forbidden)("v1")


def test_unknown_version_raises(svc):
    with pytest.raises(KeyNotFoundError):
        svc.activate("v99")
    with pytest.raises(KeyNotFoundError):
        svc.deactivate("v99")
    with pytest.raises(KeyNotFoundError):
        svc.destroy("v99")


def test_version_ids_increase_monotonically(svc):
    ids = [svc.generate_key().version_id for _ in range(3)]
    assert ids == ["v1", "v2", "v3"]
