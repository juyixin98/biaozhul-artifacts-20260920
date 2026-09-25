import bootstrap  # noqa: F401

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from anti_replay.nonce_store import MemoryNonceStore, SqliteNonceStore


class _StoreContract:
    """两种存储共用的行为契约（具体类由子类 setUp 提供）。"""

    def make_store(self, ttl=600):  # pragma: no cover - 子类覆盖
        raise NotImplementedError

    def test_first_claim_wins(self):
        store = self.make_store()
        self.assertTrue(store.claim("k1", "nonce-aaaa1111", ts=1000, now=1000))
        self.assertFalse(store.claim("k1", "nonce-aaaa1111", ts=1000, now=1001))

    def test_scope_is_per_key_id(self):
        store = self.make_store()
        self.assertTrue(store.claim("k1", "nonce-aaaa1111", ts=1000, now=1000))
        # 同一 nonce 字符串，不同 key id 互不影响
        self.assertTrue(store.claim("k2", "nonce-aaaa1111", ts=1000, now=1000))
        self.assertFalse(store.claim("k2", "nonce-aaaa1111", ts=1000, now=1001))

    def test_expired_record_allows_reuse_after_ttl(self):
        # ts=1000, ttl=600 -> 记录 1600 过期；1600 之后重放已先被时间窗拒绝，
        # 但存储本身按 ttl 清理。
        store = self.make_store(ttl=600)
        self.assertTrue(store.claim("k1", "nonce-aaaa1111", ts=1000, now=1000))
        self.assertFalse(store.claim("k1", "nonce-aaaa1111", ts=1000, now=1599))
        self.assertTrue(store.claim("k1", "nonce-aaaa1111", ts=1000, now=1600))

    def test_concurrent_duplicates_exactly_one_winner(self):
        store = self.make_store()
        n_threads = 64
        barrier = threading.Barrier(n_threads)
        results = []

        def claim():
            barrier.wait()
            results.append(store.claim("k1", "nonce-concurrent", ts=2000, now=2000))

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            list(pool.map(lambda _: claim(), range(n_threads)))

        self.assertEqual(sum(results), 1)
        self.assertEqual(len(results), n_threads)

    def test_many_distinct_nonces_all_accepted(self):
        store = self.make_store()
        for i in range(200):
            self.assertTrue(
                store.claim("k1", f"nonce-{i:08x}", ts=3000, now=3000)
            )
        # 再来一轮，全部重放
        for i in range(200):
            self.assertFalse(
                store.claim("k1", f"nonce-{i:08x}", ts=3000, now=3001)
            )


class MemoryStoreTests(_StoreContract, unittest.TestCase):
    def make_store(self, ttl=600):
        return MemoryNonceStore(ttl_seconds=ttl)


class SqliteStoreTests(_StoreContract, unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)  # 让存储自己建库

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.unlink(p)

    def make_store(self, ttl=600):
        return SqliteNonceStore(self.path, ttl_seconds=ttl)

    def test_separate_store_instances_share_database(self):
        # 模拟两个工作进程：两个连接指向同一个 db 文件
        a = SqliteNonceStore(self.path, ttl_seconds=600)
        b = SqliteNonceStore(self.path, ttl_seconds=600)
        self.assertTrue(a.claim("k1", "nonce-cross-proc", ts=1000, now=1000))
        self.assertFalse(b.claim("k1", "nonce-cross-proc", ts=1000, now=1000))
        a.close()
        b.close()


if __name__ == "__main__":
    unittest.main()
