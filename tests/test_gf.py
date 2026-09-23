"""GF(2^8) Rijndael 域的已知答案与代数性质测试。"""

import os
import unittest

from tss.gf import GF256
from tss.errors import ParameterError


class TestGF256(unittest.TestCase):
    # FIPS 197 §4.2 的已知答案向量
    KAT = [
        (0x57, 0x83, 0xC1),
        (0x57, 0x13, 0xFE),
        (0x57, 0x02, 0xAE),
        (0x53, 0xCA, 0x01),
        (0x01, 0x00, 0x00),
        (0xFF, 0x01, 0xFF),
        (0x80, 0x02, 0x1B),   # 0x80 * x 触发归约：0x100 XOR 0x11B = 0x1B
    ]

    def test_known_answer_vectors(self):
        for a, b, expected in self.KAT:
            self.assertEqual(GF256.mul(a, b), expected, f"{a:#x} * {b:#x}")

    def test_commutative(self):
        for a in range(256):
            for b in range(256):
                self.assertEqual(GF256.mul(a, b), GF256.mul(b, a))

    def test_associative(self):
        triples = [
            (0x53, 0xCA, 0x7F), (0x01, 0x02, 0x04), (0xFF, 0x80, 0x2D),
            (0x37, 0xA1, 0x9C), (0x00, 0x55, 0xAA),
        ]
        for a, b, c in triples:
            self.assertEqual(
                GF256.mul(GF256.mul(a, b), c),
                GF256.mul(a, GF256.mul(b, c)),
            )

    def test_distributive(self):
        for a, b, c in [(0x57, 0x83, 0x13), (0x00, 0x77, 0x78), (0xFF, 0x01, 0x02)]:
            self.assertEqual(
                GF256.mul(a, b ^ c),
                GF256.mul(a, b) ^ GF256.mul(a, c),
            )

    def test_inverse_all_nonzero(self):
        for a in range(1, 256):
            self.assertEqual(GF256.mul(a, GF256.inverse(a)), 1)

    def test_inverse_zero_rejected(self):
        with self.assertRaises(ParameterError):
            GF256.inverse(0)

    def test_division(self):
        self.assertEqual(GF256.div(0xC1, 0x83), 0x57)
        with self.assertRaises(ParameterError):
            GF256.div(1, 0)

    def test_pow(self):
        # 费马式性质：a^255 == 1（非零）
        for a in (1, 2, 3, 0x57, 0xFF):
            self.assertEqual(GF256.pow(a, 255), 1)

    def test_vector_ops(self):
        data = os.urandom(64)
        # 乘 0 全零，乘 1 不变
        self.assertEqual(GF256.vec_mul_scalar(data, 0), b"\x00" * 64)
        self.assertEqual(GF256.vec_mul_scalar(data, 1), data)
        # 逐字节标量乘与逐点标量乘一致
        scalar = 0x57
        self.assertEqual(
            GF256.vec_mul_scalar(data, scalar),
            bytes(GF256.mul(b, scalar) for b in data),
        )
        # XOR 两次还原
        other = os.urandom(64)
        twice = GF256.vec_xor(GF256.vec_xor(data, other), other)
        self.assertEqual(twice, data)

    def test_vector_xor_length_check(self):
        with self.assertRaises(ParameterError):
            GF256.vec_xor(b"\x01\x02", b"\x01")


if __name__ == "__main__":
    unittest.main()
