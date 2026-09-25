"""HTTP JSON API 端到端测试：真实起服务，客户端独立验证。

验收主流程在这里闭环：
追加叶子 → 拿旧 STH（验签）→ 继续追加 → 取包含/一致性证明 →
客户端本地重算根比对；再分别用**伪造根**与**错误索引**确认证明被拒绝。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from tlog.client import TransparencyLogClient
from tlog.hashing import leaf_hash
from tlog.merkle import verify_consistency, verify_inclusion
from tlog.server import make_server


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode())


class ServerE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.httpd = make_server("127.0.0.1", 0, cls._tmp.name)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = TransparencyLogClient(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls._tmp.cleanup()

    def test_01_health_and_empty_sth(self):
        status, body = _get(f"http://127.0.0.1:{self.port}/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["tree_size"], 0)

        sth = self.client.get_sth()
        self.assertEqual(sth["tree_size"], 0)
        self.assertTrue(self.client.verify_sth(sth))

    def test_02_append_leaves_non_power_of_two(self):
        for i in range(11):  # 非二次幂 11
            resp = self.client.add_leaf_utf8(f"证书或事件 #{i}")
            self.assertEqual(resp["leaf_index"], i)
        sth = self.client.get_sth()
        self.assertEqual(sth["tree_size"], 11)
        self.assertTrue(self.client.verify_sth(sth))

    def test_03_inclusion_for_every_leaf_against_sth(self):
        sth = self.client.get_sth()
        root = bytes.fromhex(sth["sha256_root_hash"])
        for i in range(11):
            leaf = self.client.get_leaf(i)
            proof = self.client.get_inclusion_proof(i)
            self.assertTrue(
                self.client.verify_inclusion_response(
                    leaf["data_utf8"].encode(), proof, root
                )
            )
            self.assertTrue(
                verify_inclusion(
                    leaf_hash(leaf["data_utf8"].encode()),
                    i,
                    11,
                    [bytes.fromhex(p) for p in proof["inclusion_path"]],
                    root,
                )
            )

    def test_04_forged_root_must_fail(self):
        sth = self.client.get_sth()
        root = bytearray(bytes.fromhex(sth["sha256_root_hash"]))
        root[0] ^= 0x01  # 伪造一个「旧根」
        proof = self.client.get_inclusion_proof(7)
        leaf = self.client.get_leaf(7)
        self.assertFalse(
            verify_inclusion(
                leaf_hash(leaf["data_utf8"].encode()),
                7,
                11,
                [bytes.fromhex(p) for p in proof["inclusion_path"]],
                bytes(root),
            )
        )

    def test_05_wrong_index_must_fail(self):
        sth = self.client.get_sth()
        root = bytes.fromhex(sth["sha256_root_hash"])
        proof = self.client.get_inclusion_proof(4)
        leaf = self.client.get_leaf(4)
        for wrong in (0, 3, 5, 10):
            self.assertFalse(
                verify_inclusion(
                    leaf_hash(leaf["data_utf8"].encode()),
                    wrong,
                    11,
                    [bytes.fromhex(p) for p in proof["inclusion_path"]],
                    root,
                )
            )

    def test_06_grow_and_verify_consistency_with_old_signed_sth(self):
        # 旧 STH：大小 11（已验签）。继续追加到 20。
        old_sth = self.client.get_sth()
        self.assertTrue(self.client.verify_sth(old_sth))
        for i in range(11, 20):
            self.client.add_leaf_utf8(f"新增条目 {i}")
        new_sth = self.client.get_sth()
        self.assertEqual(new_sth["tree_size"], 20)
        self.assertTrue(self.client.verify_sth(new_sth))

        resp = self.client.get_consistency_proof(11)
        self.assertTrue(self.client.verify_consistency_response(resp))
        self.assertTrue(
            verify_consistency(
                11,
                20,
                [bytes.fromhex(p) for p in resp["consistency_path"]],
                bytes.fromhex(old_sth["sha256_root_hash"]),
                bytes.fromhex(new_sth["sha256_root_hash"]),
            )
        )

    def test_07_forged_old_root_consistency_fails(self):
        old = self.client.get_consistency_proof(11)
        forged = bytes.fromhex(old["first_root_hash_hex"])
        forged = bytes([forged[0] ^ 0x80]) + forged[1:]
        self.assertFalse(
            verify_consistency(
                11,
                20,
                [bytes.fromhex(p) for p in old["consistency_path"]],
                forged,
                bytes.fromhex(old["second_root_hash_hex"]),
            )
        )

    def test_08_history_tree_size_inclusion(self):
        # 在大小 20 的树上索取历史大小 11 的证明，应用旧根验证
        old_sth_resp = self.client.get_consistency_proof(11)
        old_root = bytes.fromhex(old_sth_resp["first_root_hash_hex"])
        proof = self.client.get_inclusion_proof(3, tree_size=11)
        self.assertEqual(proof["tree_size"], 11)
        leaf = self.client.get_leaf(3)
        self.assertTrue(
            verify_inclusion(
                leaf_hash(leaf["data_utf8"].encode()),
                3,
                11,
                [bytes.fromhex(p) for p in proof["inclusion_path"]],
                old_root,
            )
        )

    def test_09_error_handling(self):
        base = f"http://127.0.0.1:{self.port}"
        # 越界索引
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(base + "/get-inclusion-proof?leaf_index=999")
        self.assertEqual(cm.exception.code, 400)
        # 缺参数
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(base + "/get-inclusion-proof")
        self.assertEqual(cm.exception.code, 400)
        # 一致性 first 越界
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(base + "/get-consistency-proof?first=0")
        self.assertEqual(cm.exception.code, 400)
        # 路由不存在
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(base + "/nope")
        self.assertEqual(cm.exception.code, 404)
        # 错误 JSON
        req = urllib.request.Request(
            base + "/add", data=b"not-json", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        # 缺叶子字段
        req = urllib.request.Request(
            base + "/add", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)

    def test_10_binary_leaf_roundtrip(self):
        data = bytes(range(256))
        import base64

        resp = self.client.add_leaf_bytes(data)
        idx = resp["leaf_index"]
        got = self.client.get_leaf(idx)
        self.assertEqual(base64.b64decode(got["data_b64"]), data)
        proof = self.client.get_inclusion_proof(idx)
        sth = self.client.get_sth()
        self.assertTrue(
            verify_inclusion(
                leaf_hash(data),
                idx,
                sth["tree_size"],
                [bytes.fromhex(p) for p in proof["inclusion_path"]],
                bytes.fromhex(sth["sha256_root_hash"]),
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
