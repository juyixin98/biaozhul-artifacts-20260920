"""高层流程测试：拆分/恢复、阈值拒绝、混批、损坏编码、认证模式。"""

import base64
import os
import unittest

from tss import core
from tss.envelope import generate_auth_key, decode_share


class TestSplitRecover(unittest.TestCase):
    def test_recover_any_threshold_subset(self):
        secret = b"any-quorum-of-three"
        r = core.split(secret, 3, 5)
        # 枚举所有三元子集，都必须恢复成功
        from itertools import combinations
        for combo in combinations(r.shares, 3):
            out = core.recover(list(combo))
            self.assertEqual(out.secret, secret)
            self.assertEqual(out.split_id, r.split_id)
            self.assertFalse(out.authenticated)

    def test_recover_with_extra_shares(self):
        secret = "任意阈值子集恢复成功 ✓".encode("utf-8")
        r = core.split(secret, 2, 4)
        out = core.recover(r.shares)  # 4 > 2，触发诊断
        self.assertEqual(out.secret, secret)
        self.assertEqual(out.used_share_count, 4)

    def test_binary_secret_b64(self):
        secret = os.urandom(256)
        r = core.split(secret, 2, 3)
        self.assertEqual(core.recover(r.shares).secret, secret)

    def test_below_threshold_refused(self):
        r = core.split(b"need three", 3, 5)
        with self.assertRaises(core.ThresholdError) as cm:
            core.recover(r.shares[:2])
        self.assertEqual(cm.exception.code, "below_threshold")

    def test_duplicate_x_refused(self):
        r = core.split(b"no duplicate", 3, 5)
        with self.assertRaises(core.DuplicateIndexError):
            core.recover([r.shares[0], r.shares[0], r.shares[1]])

    def test_mixed_batch_refused(self):
        a = core.split(b"batch-one", 3, 5)
        b = core.split(b"batch-two", 3, 5)
        with self.assertRaises(core.ConsistencyError) as cm:
            core.recover([a.shares[0], a.shares[1], b.shares[2]])
        self.assertEqual(cm.exception.code, "inconsistent_shares")
        # 下标 2 是另一批的份额
        self.assertIn(2, cm.exception.suspect)

    def test_corrupted_base64_is_rejected(self):
        # 1 个坏编码 + 3 个好份额（t=2）：坏份额被隔离拒绝，好份额足够 -> 恢复成功
        r = core.split(b"drop junk", 2, 4)
        out = core.recover(["@@@not-base64@@@", r.shares[1], r.shares[2], r.shares[3]])
        self.assertEqual(out.secret, b"drop junk")
        self.assertEqual(len(out.rejected), 1)
        self.assertEqual(out.rejected[0]["index"], 0)
        self.assertEqual(out.rejected[0]["reason"], "malformed_share")

    def test_corrupted_base64_leaving_below_threshold_refused(self):
        # 坏份额导致可用份额不足阈值时拒绝恢复，并在 rejected 中逐个说明
        r = core.split(b"need both", 2, 3)
        with self.assertRaises(core.ThresholdError) as cm:
            core.recover(["@@@not-base64@@@", r.shares[0]])
        self.assertEqual(cm.exception.rejected[0]["reason"], "malformed_share")

    def test_checksum_failure_is_rejected_but_recover_continues(self):
        r = core.split(b"one bad apple", 2, 4)
        raw = bytearray(base64.standard_b64decode(r.shares[0]))
        raw[30] ^= 0x01
        bad = base64.standard_b64encode(bytes(raw)).decode()
        # 坏一个、剩 3 个好份额（>= 阈值 2），仍可恢复，并在 rejected 中报告
        out = core.recover([bad, r.shares[1], r.shares[2], r.shares[3]])
        self.assertEqual(out.secret, b"one bad apple")
        self.assertEqual(len(out.rejected), 1)
        self.assertEqual(out.rejected[0]["reason"], "integrity_failure")

    def test_all_shares_bad_refused(self):
        with self.assertRaises(core.ConsistencyError):
            core.recover(["garbage", "???", ""])

    def test_parameter_mismatch_refused(self):
        a = core.split(b"x", 2, 5)
        b = core.split(b"x", 3, 5)
        with self.assertRaises(core.ConsistencyError):
            core.recover([a.shares[0], a.shares[1], b.shares[2]])

    def test_tampered_share_among_quorum_detected(self):
        # 4 个份额（t=3，n=t+1），其中 1 个载荷被恶意翻位且重算 SHA256（绕过校验和）。
        # 必须拒绝恢复；但 n < 2t-1 时无法可靠指认具体坏份额（纠错界之外），
        # 因此只断言检测到不一致，不断言 suspect 内容。
        import hashlib
        r = core.split(b"quorum attack", 3, 4)
        raw = bytearray(base64.standard_b64decode(r.shares[3]))
        raw[30] ^= 0xAA
        raw[-32:] = hashlib.sha256(bytes(raw[:-32])).digest()
        forged = base64.standard_b64encode(bytes(raw)).decode()
        with self.assertRaises(core.ConsistencyError):
            core.recover([r.shares[0], r.shares[1], r.shares[2], forged])

    def test_tampered_share_locatable_with_enough_extra_shares(self):
        # n=5 >= 2t-1（t=3）且秘密足够长：全好组合票数领先，可定位坏份额下标。
        import hashlib
        r = core.split(b"enough redundancy" * 4, 3, 5)
        raw = bytearray(base64.standard_b64decode(r.shares[4]))
        raw[30] ^= 0xAA
        raw[-32:] = hashlib.sha256(bytes(raw[:-32])).digest()
        forged = base64.standard_b64encode(bytes(raw)).decode()
        with self.assertRaises(core.ConsistencyError) as cm:
            core.recover(r.shares[:4] + [forged])
        self.assertIn(4, cm.exception.suspect)

    def test_authenticated_mode_roundtrip(self):
        key = generate_auth_key()
        r = core.split(b"mac-protected", 3, 5, auth_key=key)
        self.assertTrue(r.authenticated)
        out = core.recover(r.shares[:3], auth_key=key)
        self.assertEqual(out.secret, b"mac-protected")
        self.assertTrue(out.authenticated)

    def test_authenticated_tamper_rejected(self):
        import hashlib
        key = generate_auth_key()
        r = core.split(b"mac-protected", 3, 5, auth_key=key)
        raw = bytearray(base64.standard_b64decode(r.shares[0]))
        raw[30] ^= 0x55
        raw[-32:] = hashlib.sha256(bytes(raw[:-32])).digest()
        forged = base64.standard_b64encode(bytes(raw)).decode()
        # 伪造者没有 HMAC 密钥，该份额被标签层直接拒绝；剩余 2 个 < t=3 -> 拒绝恢复
        with self.assertRaises(core.ThresholdError):
            core.recover([forged, r.shares[1], r.shares[2]], auth_key=key)

    def test_authenticated_share_without_key_is_refused(self):
        key = generate_auth_key()
        r = core.split(b"mac-protected", 3, 5, auth_key=key)
        # 恢复方没带密钥：fail-closed，所有认证份额都不采信
        with self.assertRaises(core.ConsistencyError):
            core.recover(r.shares[:3])

    def test_validate_single_share(self):
        r = core.split(b"inspect", 2, 3)
        info = core.validate(r.shares[0])
        self.assertEqual(info["version"], 1)
        self.assertEqual(info["threshold"], 2)
        self.assertEqual(info["total"], 3)
        self.assertEqual(info["split_id"], r.split_id)
        self.assertEqual(info["payload_bytes"], len(b"inspect"))

    def test_empty_secret_and_bad_params(self):
        with self.assertRaises(core.ParameterError):
            core.split(b"", 2, 3)
        with self.assertRaises(core.ParameterError):
            core.split(b"x", 1, 3)
        with self.assertRaises(core.ParameterError):
            core.split(b"x", 3, 2)
        with self.assertRaises(core.ParameterError):
            core.split(b"x", 2, 256)
        with self.assertRaises(core.ParameterError):
            core.recover([])


if __name__ == "__main__":
    unittest.main()
