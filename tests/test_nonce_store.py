import os
import tempfile
import threading
import unittest

from replay_protection.nonce_store import NonceStore


class TestNonceStore(unittest.TestCase):
    def setUp(self):
        self.store = NonceStore(":memory:", window_seconds=300)

    def tearDown(self):
        self.store.close()

    def test_register_once(self):
        self.assertTrue(self.store.register("kid", "nonce-aaaaaaaaaaaa", 1000, now=1100))
        self.assertFalse(self.store.register("kid", "nonce-aaaaaaaaaaaa", 1000, now=1100))

    def test_nonce_scoped_per_kid(self):
        self.assertTrue(self.store.register("k1", "nonce-aaaaaaaaaaaa", 1000, now=1100))
        self.assertTrue(self.store.register("k2", "nonce-aaaaaaaaaaaa", 1000, now=1100))

    def test_reuse_after_window_expiry(self):
        # expires_at = timestamp + 300 = 1300. At now=1300 still held
        # (boundary inclusive); at now=1301 the nonce is reusable.
        self.assertTrue(self.store.register("kid", "nonce-aaaaaaaaaaaa", 1000, now=1000))
        self.assertFalse(self.store.register("kid", "nonce-aaaaaaaaaaaa", 1000, now=1300))
        self.assertTrue(self.store.register("kid", "nonce-aaaaaaaaaaaa", 1301, now=1301))

    def test_purge(self):
        self.store.register("kid", "nonce-aaaaaaaaaaaa", 1000, now=1000)
        self.assertEqual(self.store.purge_expired(now=1300), 0)
        self.assertEqual(self.store.purge_expired(now=1301), 1)
        self.assertEqual(self.store.count(), 0)

    def test_concurrent_duplicates_single_winner(self):
        store = NonceStore(":memory:", window_seconds=300)
        results = []
        barrier = threading.Barrier(32)

        def attempt():
            barrier.wait()
            results.append(store.register("kid", "race-nonce-0123456789", 1000, now=1000))

        threads = [threading.Thread(target=attempt) for _ in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 31)
        store.close()

    def test_file_backed_store_persists(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "n.db")
            s1 = NonceStore(db, window_seconds=300)
            self.assertTrue(s1.register("kid", "nonce-aaaaaaaaaaaa", 1000, now=1000))
            s1.close()
            s2 = NonceStore(db, window_seconds=300)
            self.assertFalse(s2.register("kid", "nonce-aaaaaaaaaaaa", 1000, now=1000))
            s2.close()

    def test_file_backed_concurrent_processes(self):
        """Two real OS processes racing on one db file: exactly one insert."""
        import textwrap

        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "n.db")
            worker = textwrap.dedent(
                f"""
                import sys
                sys.path.insert(0, {os.getcwd()!r})
                from replay_protection.nonce_store import NonceStore
                import time
                s = NonceStore({db!r})
                # crude barrier: both processes try within the same moment
                time.sleep(0.2)
                ok = s.register('kid', 'proc-race-nonce-000001', 1000, now=1000)
                print('WIN' if ok else 'LOSE')
                s.close()
                """
            )
            import subprocess
            import sys

            procs = [
                subprocess.Popen([sys.executable, "-c", worker], stdout=subprocess.PIPE)
                for _ in range(2)
            ]
            outs = [p.communicate(timeout=30)[0].decode().strip() for p in procs]
            self.assertEqual(sorted(outs), ["LOSE", "WIN"])


if __name__ == "__main__":
    unittest.main()
