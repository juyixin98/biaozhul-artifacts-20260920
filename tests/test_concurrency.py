"""并发测试：多线程交错轮换/加解密，状态机始终保持不变量。"""

from __future__ import annotations

import threading

from keyversion.service import KeyService
from keyversion.store import ACTIVE, KeyStore


def test_concurrent_rotates_leave_exactly_one_active(tmp_path):
    store = KeyStore(tmp_path / "ks").open()
    svc = KeyService(store)
    svc.rotate()
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(10):
                svc.rotate()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    actives = [v for v in svc.list_versions() if v.state == ACTIVE]
    assert len(actives) == 1
    assert store.active_version_id() == actives[0].version_id
    # 1 初始 + 60 次轮换
    assert len(svc.list_versions()) == 61
    store.close()


def test_concurrent_encrypt_while_rotating_all_ciphertexts_decryptable(tmp_path):
    store = KeyStore(tmp_path / "ks").open()
    svc = KeyService(store)
    svc.rotate()
    ciphertexts: list[tuple[bytes, str]] = []
    lock = threading.Lock()
    errors: list[Exception] = []

    def encryptor(idx: int) -> None:
        try:
            for i in range(20):
                ct, info = svc.encrypt(f"t{idx}-{i}".encode())
                with lock:
                    ciphertexts.append((ct, info.version_id))
        except Exception as exc:  # 轮换间隙不应产生加密错误（总有一个 active）
            errors.append(exc)

    def rotator() -> None:
        try:
            for _ in range(15):
                svc.rotate()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    enc_threads = [threading.Thread(target=encryptor, args=(i,)) for i in range(4)]
    rot_thread = threading.Thread(target=rotator)
    for t in enc_threads:
        t.start()
    rot_thread.start()
    for t in enc_threads:
        t.join()
    rot_thread.join()

    assert not errors, errors
    # 所有密文都能按其版本解密（旧版本此时为 retired，仍可解密）
    for ct, vid in ciphertexts:
        pt, used = svc.decrypt(ct)
        assert used == vid
        assert pt.startswith(b"t")
    store.close()


def test_concurrent_lifecycle_mix_preserves_invariants(tmp_path):
    """混合 generate/activate/deactivate/rotate/destroy 并发后重新打开，状态仍自洽。

    交错执行时，某个版本可能已被其他线程的 activate/rotate 停用，此时再
    deactivate 会得到 InvalidStateTransition —— 这是状态机正确的拒绝行为，
    不是损坏。这里容忍“竞争导致的预期拒绝”，只严格校验状态不变量。
    """
    from keyversion.errors import InvalidStateTransition

    store = KeyStore(tmp_path / "ks").open()
    svc = KeyService(store)
    svc.rotate()
    benign: list[Exception] = []
    fatal: list[Exception] = []

    def lifecycle_worker() -> None:
        try:
            for _ in range(8):
                g = svc.generate()
                try:
                    svc.activate(g.version_id)
                    # 可能已被别的线程停用（竞争）
                    try:
                        svc.deactivate(g.version_id)
                    except InvalidStateTransition:
                        pass
                    svc.destroy(g.version_id)
                except InvalidStateTransition as exc:
                    benign.append(exc)
        except Exception as exc:  # noqa: BLE001
            fatal.append(exc)

    def rotate_worker() -> None:
        try:
            for _ in range(8):
                svc.rotate()
        except Exception as exc:  # noqa: BLE001
            fatal.append(exc)

    threads = [threading.Thread(target=lifecycle_worker) for _ in range(3)]
    threads.append(threading.Thread(target=rotate_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not fatal, fatal

    actives = [v for v in svc.list_versions() if v.state == ACTIVE]
    assert len(actives) <= 1
    if actives:
        assert store.active_version_id() == actives[0].version_id
    for v in svc.list_versions():
        if v.state == "destroyed":
            assert v.has_material is False
    store.close()

    # 重新打开必须通过完整性与审计链校验
    s2 = KeyStore(tmp_path / "ks").open()
    s2.close()
