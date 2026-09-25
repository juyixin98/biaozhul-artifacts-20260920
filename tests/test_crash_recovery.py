"""崩溃一致性测试（验收项）：子进程运行中被 SIGKILL，重开后状态必须一致。

覆盖：
1. 硬杀死正在轮换/加解密的进程，重开密钥库：
   - state.json 可解析、不变量成立（至多一个 active、destroyed 无材料）
   - 审计哈希链有效（末尾允许丢最后一条）
   - 已落盘的密文：对应版本 active/retired 时可解且明文正确，
     其余（如未及记录的边界）按版本状态给出明确拒绝。
2. 多轮“杀 -> 重开 -> 继续”恢复后仍然成立。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from keyversion.errors import DecryptVersionDestroyed, InvalidEnvelope, VersionNotFound
from keyversion.service import KeyService
from keyversion.store import ACTIVE, KeyStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _spawn_worker(data_dir: Path, out_file: Path, iterations: int = 100000) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests._crash_worker",
            str(data_dir),
            str(iterations),
            str(out_file),
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _kill_after(proc: subprocess.Popen, delay: float) -> bytes:
    time.sleep(delay)
    stderr = b""
    if proc.poll() is None:
        proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    if proc.stderr is not None:
        stderr = proc.stderr.read()
        proc.stderr.close()
    return stderr


def _load_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # SIGKILL 可能留下末尾半行：跳过（上面的完整行均已 fsync）
            continue
    return records


def _assert_state_invariants(svc: KeyService) -> None:
    versions = svc.list_versions()
    actives = [v for v in versions if v.state == ACTIVE]
    assert len(actives) <= 1
    if actives:
        assert svc.active_version().version_id == actives[0].version_id
    for v in versions:
        if v.state == "destroyed":
            assert v.has_material is False
        else:
            assert v.has_material is True
    # 审计链必须可验证通过
    entries = svc.list_audit()
    assert [e.seq for e in entries] == list(range(1, len(entries) + 1))
    for i, e in enumerate(entries):
        assert e.prev_hash == ("" if i == 0 else entries[i - 1].entry_hash)


@pytest.mark.slow
def test_sigkill_during_rotation_and_requests(tmp_path):
    data_dir = tmp_path / "ks"
    records_file = tmp_path / "records.jsonl"

    # 第一轮：先初始化，然后关闭释放文件锁
    init = KeyStore(data_dir).open()
    KeyService(init).rotate()
    init.close()

    last_record_count = -1
    for round_no in range(3):
        proc = _spawn_worker(data_dir, records_file)
        try:
            # 在不同时机杀死：早 / 中 / 晚
            stderr = _kill_after(proc, {0: 0.2, 1: 0.6, 2: 1.1}[round_no])
        finally:
            pass
        assert proc.returncode == -signal.SIGKILL, f"worker not killed: {stderr[-400:].decode(errors='replace')}"

        # 重开：完整性 + 审计链校验在 open() 内完成，能打开即通过
        store = KeyStore(data_dir).open()
        svc = KeyService(store)
        _assert_state_invariants(svc)

        # 逐条核对已落盘记录
        records = _load_records(records_file)
        assert len(records) >= 1, "worker 被杀死前至少应完成一轮轮换+加密"
        decrypted_ok = 0
        for rec in records:
            envelope = KeyService.b64d(rec["ciphertext"])
            state = None
            try:
                state = store.version_state(rec["version_id"])
            except VersionNotFound:
                pytest.fail("记录指向的版本在状态中不存在（记录文件原子写，不该发生）")
            if state in ("active", "retired"):
                pt, vid = svc.decrypt(envelope)
                assert vid == rec["version_id"]
                assert pt.decode() == rec["plaintext"]
                decrypted_ok += 1
            elif state == "destroyed":
                # worker 不销毁版本；出现即失败
                pytest.fail("worker 不应该销毁版本")
            else:
                pytest.fail(f"记录版本处于意外状态 {state}")
        assert decrypted_ok == len(records)
        assert len(records) > last_record_count
        last_record_count = len(records)

        # 审计文件字节中不得包含明文标记
        audit_bytes = (data_dir / "audit.log").read_bytes()
        assert b"crash-record-" not in audit_bytes
        store.close()

    print(f"\n三轮崩溃恢复后累计记录 {last_record_count} 条，全部可按版本解密")


@pytest.mark.slow
def test_sigkill_then_worker_can_continue_and_finish(tmp_path):
    """杀死后再启动的 worker 能干净地跑完，最终所有记录可解密。"""
    data_dir = tmp_path / "ks2"
    records_file = tmp_path / "records2.jsonl"
    init = KeyStore(data_dir).open()
    KeyService(init).rotate()
    init.close()  # 父进程必须释放文件锁，子进程才能获取

    proc = _spawn_worker(data_dir, records_file)
    _kill_after(proc, 0.4)
    assert proc.returncode == -signal.SIGKILL

    # 恢复并正常跑完少量迭代
    proc2 = _spawn_worker(data_dir, records_file, iterations=5)
    proc2.wait(timeout=30)
    assert proc2.returncode == 0, proc2.stderr.read().decode()[-800:]

    store = KeyStore(data_dir).open()
    svc = KeyService(store)
    _assert_state_invariants(svc)
    for rec in _load_records(records_file):
        pt, vid = svc.decrypt(KeyService.b64d(rec["ciphertext"]))
        assert vid == rec["version_id"] and pt.decode() == rec["plaintext"]
    store.close()
