"""Log 追加存储：哈希链防篡改、持久化重开、证明生成集成测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tlog.hashing import EMPTY_TREE_HASH, leaf_hash
from tlog.log import CorruptLogError, Log
from tlog.merkle import verify_consistency, verify_inclusion


class LogStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _open(self) -> Log:
        return Log(self.dir)

    def test_empty_log_sth(self):
        log = self._open()
        self.assertEqual(log.size, 0)
        self.assertEqual(log.root(), EMPTY_TREE_HASH)
        sth = log.get_sth(timestamp_ms=1)
        self.assertEqual(sth["tree_size"], 0)
        self.assertEqual(sth["sha256_root_hash"], EMPTY_TREE_HASH.hex())
        self.assertTrue(log.verify_sth(sth))

    def test_append_returns_sequential_indices(self):
        log = self._open()
        for i in range(5):
            self.assertEqual(log.append(f"叶子{i}".encode(), added_ms=i + 1), i)
        self.assertEqual(log.size, 5)

    def test_persistence_across_reopen_and_proofs(self):
        log = self._open()
        for i in range(9):  # 非二次幂
            log.append(f"data-{i}".encode(), added_ms=1000 + i)
        root_before = log.root()
        del log

        log2 = self._open()  # 重放 leaves.jsonl
        self.assertEqual(log2.size, 9)
        self.assertEqual(log2.root(), root_before)
        for i in range(9):
            lh, proof, root, n = log2.inclusion_proof(i)
            self.assertEqual(n, 9)
            self.assertEqual(lh, leaf_hash(f"data-{i}".encode()))
            self.assertTrue(verify_inclusion(lh, i, n, proof, root))
        # 重开后继续追加
        self.assertEqual(log2.append(b"data-9", added_ms=2000), 9)

    def test_key_files_created_and_reused(self):
        log = self._open()
        pub1 = log.get_sth()["public_key_hex"]
        del log
        log2 = self._open()
        self.assertEqual(log2.get_sth()["public_key_hex"], pub1)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "test_ed25519_private.pem")))

    def test_consistency_after_appends(self):
        log = self._open()
        for i in range(10):
            log.append(f"x{i}".encode(), added_ms=i)
        for first in (1, 3, 4, 7, 9):
            old, new, proof, fn, sn = log.consistency_proof(first)
            self.assertTrue(verify_consistency(fn, sn, proof, old, new))

    def test_sth_signature_binds_root_size_timestamp(self):
        log = self._open()
        log.append(b"hello", added_ms=1)
        sth = log.get_sth(timestamp_ms=42)
        self.assertTrue(log.verify_sth(sth))
        forged = dict(sth)
        forged["tree_size"] = 2
        self.assertFalse(log.verify_sth(forged))
        forged = dict(sth)
        forged["sha256_root_hash"] = "00" * 32
        self.assertFalse(log.verify_sth(forged))

    def test_old_signed_root_remains_valid_after_growth(self):
        """验收场景：先记录旧 STH（含签名），追加后用一致性证明连接新旧根。"""
        log = self._open()
        for i in range(5):
            log.append(f"old-{i}".encode(), added_ms=i)
        old_sth = log.get_sth(timestamp_ms=100)
        self.assertTrue(log.verify_sth(old_sth))

        for i in range(5, 13):
            log.append(f"new-{i}".encode(), added_ms=i)
        new_sth = log.get_sth(timestamp_ms=200)

        old_root = bytes.fromhex(old_sth["sha256_root_hash"])
        new_root = bytes.fromhex(new_sth["sha256_root_hash"])
        _, _, proof, first, second = log.consistency_proof(old_sth["tree_size"])
        self.assertEqual(first, 5)
        self.assertEqual(second, 13)
        self.assertTrue(verify_consistency(first, second, proof, old_root, new_root))

    # ----------------------------------------------------- 哈希链防篡改

    def _records_file(self) -> str:
        return os.path.join(self.dir, "leaves.jsonl")

    def test_tampered_leaf_content_detected(self):
        log = self._open()
        log.append(b"original", added_ms=1)
        log.append(b"keep", added_ms=2)
        lines = open(self._records_file(), encoding="utf-8").read().splitlines()
        rec = json.loads(lines[0])
        rec["leaf_b64"] = "b3JpZ2luYWw="  # 仍是合法 base64，内容同形时不会触发；下面改成别的
        rec["leaf_b64"] = __import__("base64").b64encode(b"TAMPERED").decode()
        # 不更新 record_hash_hex —— 重放必须报错
        lines[0] = json.dumps(rec, sort_keys=True)
        with open(self._records_file(), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        with self.assertRaises(CorruptLogError):
            self._open()

    def test_inserted_record_detected(self):
        log = self._open()
        log.append(b"a", added_ms=1)
        log.append(b"b", added_ms=2)
        lines = open(self._records_file(), encoding="utf-8").read().splitlines()
        # 在中间插入一条伪造记录（其 prev_hash 与链不匹配）
        fake = {
            "leaf_b64": "ZmFrZQ==",
            "added_ms": 1,
            "prev_hash_hex": "00" * 32,
            "record_hash_hex": "11" * 32,
        }
        lines.insert(1, json.dumps(fake, sort_keys=True))
        with open(self._records_file(), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        with self.assertRaises(CorruptLogError):
            self._open()

    def test_deleted_record_detected(self):
        log = self._open()
        for i in range(3):
            log.append(f"r{i}".encode(), added_ms=i)
        lines = open(self._records_file(), encoding="utf-8").read().splitlines()
        del lines[1]
        with open(self._records_file(), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        with self.assertRaises(CorruptLogError):
            self._open()

    def test_append_rejects_non_bytes(self):
        log = self._open()
        with self.assertRaises(TypeError):
            log.append("not bytes")  # type: ignore[arg-type]

    def test_inclusion_for_history_size_supported(self):
        log = self._open()
        for i in range(10):
            log.append(f"h{i}".encode(), added_ms=i)
        lh, proof, root, n = log.inclusion_proof(2, tree_size=6)
        self.assertEqual(n, 6)
        self.assertTrue(verify_inclusion(lh, 2, 6, proof, root))
        with self.assertRaises(IndexError):
            log.inclusion_proof(6, tree_size=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
