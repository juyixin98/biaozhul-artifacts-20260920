"""密钥版本状态机测试：生成、激活、停用、销毁与非法转换。"""

from __future__ import annotations

import pytest

from keyversion.errors import InvalidStateTransition
from keyversion.store import ACTIVE, DESTROYED, GENERATED, RETIRED


def test_generate_leaves_version_generated_and_not_active(service):
    info = service.generate()
    assert info.state == GENERATED
    assert info.active is False
    assert service.active_version() is None


def test_rotate_creates_active_and_retires_previous(initialized_service):
    service = initialized_service
    v1 = service.active_version()
    assert v1.state == ACTIVE
    v2 = service.rotate()
    assert v2.state == ACTIVE and v2.active
    assert service.get_version(v1.version_id).state == RETIRED
    assert service.active_version().version_id == v2.version_id


def test_exactly_one_active_after_many_rotates(initialized_service):
    service = initialized_service
    for _ in range(5):
        service.rotate()
    actives = [v for v in service.list_versions() if v.state == ACTIVE]
    assert len(actives) == 1
    assert actives[0].version_id == service.active_version().version_id


def test_activate_generated_version_deactivates_old_active(initialized_service):
    service = initialized_service
    v1 = service.active_version()
    new = service.generate()  # generated
    service.activate(new.version_id)
    assert service.get_version(new.version_id).state == ACTIVE
    assert service.get_version(v1.version_id).state == RETIRED
    assert service.active_version().version_id == new.version_id


def test_activate_is_idempotent_when_already_active(initialized_service):
    service = initialized_service
    v1 = service.active_version()
    before = len(service.list_audit())
    service.activate(v1.version_id)
    after = service.list_audit()
    assert service.active_version().version_id == v1.version_id
    # 幂等只追加一条 noop 审计
    assert len(after) == before + 1
    assert after[-1].result == "noop"


def test_illegal_transitions_rejected(initialized_service):
    service = initialized_service
    v1 = service.active_version()
    generated = service.generate()
    service.rotate()  # v1 -> retired

    # retired 不能直接 activate
    with pytest.raises(InvalidStateTransition):
        service.activate(v1.version_id)
    # retired 不能再次 deactivate
    with pytest.raises(InvalidStateTransition):
        service.deactivate(v1.version_id)
    # generated 不能 deactivate
    with pytest.raises(InvalidStateTransition):
        service.deactivate(generated.version_id)
    # destroyed 是终态
    service.destroy(v1.version_id)
    with pytest.raises(InvalidStateTransition):
        service.activate(v1.version_id)
    with pytest.raises(InvalidStateTransition):
        service.deactivate(v1.version_id)
    assert service.get_version(v1.version_id).state == DESTROYED


def test_destroy_active_clears_active_pointer(initialized_service):
    service = initialized_service
    v1 = service.active_version()
    service.destroy(v1.version_id)
    assert service.active_version() is None
    assert service.get_version(v1.version_id).has_material is False


def test_destroy_unknown_version_raises(service):
    with pytest.raises(Exception):
        service.destroy("v9999-nope")


def test_version_ids_unique_and_ordered(initialized_service):
    service = initialized_service
    ids = [v.version_id for v in service.list_versions()]
    for _ in range(3):
        ids.append(service.generate().version_id)
    assert len(set(ids)) == len(ids)
    seqs = [v.seq for v in service.list_versions()]
    assert seqs == sorted(seqs)
