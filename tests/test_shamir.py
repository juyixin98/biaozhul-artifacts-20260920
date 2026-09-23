"""Shamir 核心算法测试：阈值恢复、不足阈值拒绝、重复横坐标、诊断。"""

import os
import unittest
from itertools import combinations

from tss.shamir import (
    SplitParams,
    SharePoint,
    split_secret,
    combine_points,
    diagnose_points,
)
from tss.errors import (
    ParameterError,
    ThresholdError,
    DuplicateIndexError,
    ConsistencyError,
)


class TestSplitParams(unittest.TestCase):
    def test_valid(self):
        SplitParams(threshold=2, total=3).validate()
        SplitParams(threshold=1, total=1)  # 构造不报错，由 validate 约束

    def test_invalid(self):
        with self.assertRaises(ParameterError):
            SplitParams(threshold=1, total=1).validate()
        with self.assertRaises(ParameterError):
            SplitParams(threshold=3, total=2).validate()
        with self.assertRaises(ParameterError):
            SplitParams(threshold=2, total=256).validate()


class TestShamirRecover(unittest.TestCase):
    def test_linear_recover_known(self):
        # P(x) = 0x2B + 0x07 x：P(1)=0x2B^0x07=0x2C，P(2)=0x2B^(0x07*2)=0x2B^0x0E=0x25
        points = [SharePoint(1, bytes([0x2C])), SharePoint(2, bytes([0x25]))]
        self.assertEqual(combine_points(points, 2), bytes([0x2B]))

    def test_threshold2_all_pairs(self):
        secret = b"threshold-two-secret"
        points = split_secret(secret, SplitParams(2, 5))
        for pair in combinations(points, 2):
            self.assertEqual(combine_points(list(pair), 2), secret)

    def test_threshold3_all_triples(self):
        secret = os.urandom(100)
        points = split_secret(secret, SplitParams(3, 5))
        for triple in combinations(points, 3):
            self.assertEqual(combine_points(list(triple), 3), secret)

    def test_threshold_n_equals_t(self):
        secret = b"\x00\x01\x02\xff\xfe"
        points = split_secret(secret, SplitParams(4, 4))
        self.assertEqual(combine_points(points, 4), secret)

    def test_max_255_shares(self):
        secret = b"x" * 16
        points = split_secret(secret, SplitParams(2, 255))
        self.assertEqual(len(points), 255)
        self.assertEqual(combine_points(points[-2:], 2), secret)

    def test_secret_with_all_byte_values(self):
        secret = bytes(range(256)) * 4
        points = split_secret(secret, SplitParams(3, 5))
        self.assertEqual(combine_points(points[:3], 3), secret)
        self.assertEqual(combine_points(points[2:5], 3), secret)

    def test_empty_secret_rejected(self):
        with self.assertRaises(ParameterError):
            split_secret(b"", SplitParams(2, 3))

    def test_below_threshold_rejected(self):
        points = split_secret(b"need-three", SplitParams(3, 5))
        with self.assertRaises(ThresholdError):
            combine_points(points[:2], 3)

    def test_duplicate_x_rejected(self):
        points = split_secret(b"no-dupes", SplitParams(3, 5))
        bad = [points[0], points[0], points[1]]
        with self.assertRaises(DuplicateIndexError):
            combine_points(bad, 3)

    def test_x_range_enforced(self):
        with self.assertRaises(ParameterError):
            SharePoint(0, b"abc")
        with self.assertRaises(ParameterError):
            SharePoint(256, b"abc")

    def test_payload_length_mismatch(self):
        points = split_secret(b"same-length", SplitParams(3, 5))
        bad = [points[0], SharePoint(points[1].x, points[1].y + b"\x00"), points[2]]
        with self.assertRaises(ParameterError):
            combine_points(bad, 3)


class TestDiagnose(unittest.TestCase):
    def test_exact_threshold_cannot_diagnose(self):
        secret = b"diagnose-me"
        points = split_secret(secret, SplitParams(3, 5))
        recovered, suspect, votes, exhaustive = diagnose_points(points[:3], 3)
        self.assertEqual(recovered, secret)
        self.assertEqual(suspect, [])
        self.assertTrue(exhaustive)

    def test_extra_consistent_shares(self):
        secret = b"diagnose-me"
        points = split_secret(secret, SplitParams(3, 5))
        recovered, suspect, votes, exhaustive = diagnose_points(points, 3)
        self.assertEqual(recovered, secret)
        self.assertEqual(suspect, [])

    def test_one_corrupted_share_detected(self):
        # 4 个真份额 + 1 个被破坏的份额：多数票应定位到下标 4
        secret = b"find-the-bad-one"
        points = list(split_secret(secret, SplitParams(3, 5)))
        y = bytearray(points[4].y)
        y[0] ^= 0xFF
        points[4] = SharePoint(points[4].x, bytes(y))

        with self.assertRaises(ConsistencyError) as cm:
            diagnose_points(points, 3)
        self.assertIn(4, cm.exception.suspect)
        self.assertEqual(cm.exception.votes[secret.hex()], 4)  # 4 个全好组合

    def test_more_than_enough_good_with_one_bad(self):
        # 2-of-3，2 好 1 坏：能检测到不一致并拒绝恢复，但 n=t+1 < 2t
        # （Reed-Solomon 纠错界之外），密码学上无法可靠指认哪个是坏份额。
        secret = b"two-of-three"
        points = list(split_secret(secret, SplitParams(2, 3)))
        y = bytearray(points[2].y)
        y[1] ^= 0x01
        points[2] = SharePoint(points[2].x, bytes(y))

        with self.assertRaises(ConsistencyError):
            diagnose_points(points, 2)

    def test_diagnose_requires_threshold(self):
        points = split_secret(b"x", SplitParams(3, 5))
        with self.assertRaises(ThresholdError):
            diagnose_points(points[:2], 3)


if __name__ == "__main__":
    unittest.main()
