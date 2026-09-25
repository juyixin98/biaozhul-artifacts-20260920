import bootstrap  # noqa: F401

import json
import os
import stat
import tempfile
import unittest

from anti_replay.keys import KeystoreError, create_or_update_key, load_keystore


class KeystoreTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_create_load_roundtrip_and_0600_mode(self):
        secret = create_or_update_key(self.path, "k1")
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

        loaded = load_keystore(self.path)
        self.assertEqual(loaded, {"k1": secret})
        self.assertEqual(len(secret), 32)

    def test_duplicate_id_requires_overwrite(self):
        create_or_update_key(self.path, "k1")
        with self.assertRaises(KeystoreError):
            create_or_update_key(self.path, "k1")
        secret2 = create_or_update_key(self.path, "k1", overwrite=True)
        self.assertEqual(load_keystore(self.path)["k1"], secret2)

    def test_world_readable_keystore_rejected(self):
        create_or_update_key(self.path, "k1")
        os.chmod(self.path, 0o644)
        with self.assertRaises(KeystoreError):
            load_keystore(self.path)

    def test_missing_file(self):
        with self.assertRaises(KeystoreError):
            load_keystore(self.path + ".nope")

    def test_corrupt_json_rejected(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("{not json")
        with self.assertRaises(KeystoreError):
            load_keystore(self.path)

    def test_bad_version_rejected(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 99, "keys": {}}, f)
        with self.assertRaises(KeystoreError):
            load_keystore(self.path)

    def test_short_key_rejected_with_clean_error(self):
        # 审查发现 #3：过短的 num_bytes 应抛 KeystoreError（keygen 能干净报错），
        # 而不是裸 ValueError/traceback。
        with self.assertRaises(KeystoreError):
            create_or_update_key(self.path, "short", num_bytes=8)


if __name__ == "__main__":
    unittest.main()
