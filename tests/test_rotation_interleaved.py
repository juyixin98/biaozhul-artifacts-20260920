"""交错轮换与请求：模拟真实使用中"边轮换边收发"的场景。"""

import random

import pytest

from keyvault import KeyDestroyedError, NoActiveKeyError


def test_interleaved_rotation_and_requests(svc):
    """多轮轮换中持续加解密，验证历史密文的解密权限随状态变化。"""
    ciphertexts = []  # (版本号, 信封, 明文)

    # 第 1 代密钥上线
    svc.generate_key()
    svc.activate("v1")
    for i in range(3):
        pt = f"gen1-msg-{i}".encode()
        ciphertexts.append(("v1", svc.encrypt(pt), pt))

    # 轮换到 v2、v3，每代都加密新数据
    for gen in (2, 3):
        svc.generate_key()
        svc.activate(f"v{gen}")
        for i in range(3):
            pt = f"gen{gen}-msg-{i}".encode()
            ciphertexts.append((f"v{gen}", svc.encrypt(pt), pt))

    # 所有历史密文（v1/v2 已停用、v3 激活）都应可解密
    for vid, env, pt in ciphertexts:
        assert svc.decrypt(env) == pt, f"{vid} 的历史密文应可解密"

    # 新加密只能落在激活版本 v3 上
    assert svc.encrypt(b"fresh")["v"] == "v3"

    # 销毁 v1：v1 的密文立即不可恢复，其余不受影响
    svc.destroy("v1")
    for vid, env, pt in ciphertexts:
        if vid == "v1":
            with pytest.raises(KeyDestroyedError):
                svc.decrypt(env)
        else:
            assert svc.decrypt(env) == pt


def test_random_interleaved_workload(svc):
    """随机交错 生成/激活/停用/销毁/加密/解密，校验不变量。"""
    rng = random.Random(20260924)
    live = {}       # 版本号 -> 该版本加密的 (信封, 明文) 列表
    destroyed = set()
    next_id = 0

    for step in range(200):
        op = rng.choice(
            ["generate", "activate", "deactivate", "destroy", "encrypt", "decrypt"]
        )
        active = svc.status()["active_version"]

        if op == "generate":
            next_id += 1
            meta = svc.generate_key()
            assert meta.version_id == f"v{next_id}"
            live[meta.version_id] = []

        elif op == "activate" and next_id > 0:
            vid = f"v{rng.randint(1, next_id)}"
            state = svc.store.get_metadata(vid).state
            try:
                svc.activate(vid)
                assert state in ("GENERATED", "DEACTIVATED")
            except Exception:
                assert state in ("ACTIVE", "DESTROYED")

        elif op == "deactivate" and active:
            svc.deactivate(active)

        elif op == "destroy" and next_id > 0:
            vid = f"v{rng.randint(1, next_id)}"
            state = svc.store.get_metadata(vid).state
            try:
                svc.destroy(vid)
                assert state in ("GENERATED", "DEACTIVATED")
                destroyed.add(vid)
            except Exception:
                assert state in ("ACTIVE", "DESTROYED")

        elif op == "encrypt":
            try:
                pt = f"step-{step}".encode()
                env = svc.encrypt(pt)
                assert active is not None and env["v"] == active
                live[active].append((env, pt))
            except NoActiveKeyError:
                assert active is None

        elif op == "decrypt" and next_id > 0:
            vid = f"v{rng.randint(1, next_id)}"
            for env, pt in live.get(vid, []):
                if vid in destroyed:
                    with pytest.raises(KeyDestroyedError):
                        svc.decrypt(env)
                else:
                    assert svc.decrypt(env) == pt

    # 终态全量校验：未销毁版本的全部密文可解，已销毁的全部拒绝
    for vid, items in live.items():
        for env, pt in items:
            if vid in destroyed:
                with pytest.raises(KeyDestroyedError):
                    svc.decrypt(env)
            else:
                assert svc.decrypt(env) == pt
    # 审计链在 200 步随机操作后仍完整
    ok, reason = svc.audit.verify()
    assert ok, reason


def test_encrypt_always_uses_current_active(svc):
    """每次加密都必须落在当时的激活版本上。"""
    for gen in range(1, 5):
        svc.generate_key()
        svc.activate(f"v{gen}")
        for _ in range(5):
            assert svc.encrypt(b"x")["v"] == f"v{gen}"
