"""GF(2^8) 有限域，Rijndael/AES 不可约多项式。

域定义：GF(2)[x] / (x^8 + x^4 + x^3 + x + 1)，即模 0x11B。
该域是 AES 的标准域，属于公开、经过充分分析的成熟参数，本项目不自创域参数。

查表实现（生成元 g=3，阶为 255；注意 2 在该域中不是本原元，阶仅 51）。

FIPS 197 §4.2 已知答案测试向量（用于单元测试）：
    0x57 * 0x83 = 0xC1
    0x57 * 0x13 = 0xFE
    0x57 * 0x02 = 0xAE
    0x53 * 0xCA = 0x01   （互逆）
    0x01 * 0x00 = 0x00
"""

from .errors import ParameterError

_MODULUS = 0x11B
_GENERATOR = 3
_ORDER = 256


def _build_tables():
    # 注意：在 Rijndael 域中 2 不是本原元（阶为 51），生成元取 g=3（阶 255）。
    # 由 g 的幂构造完整的 exp/log 表。
    exp = [0] * (2 * (_ORDER - 1))
    log = [0] * _ORDER

    def _mul_by_3(v: int) -> int:
        doubled = v << 1
        if doubled & 0x100:
            doubled ^= _MODULUS
        return v ^ doubled  # 3v = v XOR 2v

    value = 1
    for i in range(_ORDER - 1):
        exp[i] = value
        log[value] = i
        value = _mul_by_3(value)
    for i in range(_ORDER - 1, 2 * (_ORDER - 1)):
        exp[i] = exp[i - (_ORDER - 1)]
    return exp, log


_EXP, _LOG = _build_tables()


class GF256:
    """Rijndael 域上的字节级运算与字节串向量运算。"""

    MODULUS = _MODULUS
    GENERATOR = _GENERATOR

    @staticmethod
    def add(a: int, b: int) -> int:
        """特征 2 域中加法即异或。"""
        return a ^ b

    @staticmethod
    def mul(a: int, b: int) -> int:
        if a == 0 or b == 0:
            return 0
        return _EXP[_LOG[a] + _LOG[b]]

    @staticmethod
    def inverse(a: int) -> int:
        """乘法逆元；0 没有逆元。"""
        if a == 0:
            raise ParameterError("0 has no multiplicative inverse in GF(2^8)")
        return _EXP[(_ORDER - 1) - _LOG[a]]

    @staticmethod
    def div(a: int, b: int) -> int:
        if b == 0:
            raise ParameterError("division by zero in GF(2^8)")
        if a == 0:
            return 0
        return _EXP[(_LOG[a] - _LOG[b]) % (_ORDER - 1)]

    @staticmethod
    def pow(a: int, n: int) -> int:
        if a == 0:
            return 0
        return _EXP[(_LOG[a] * n) % (_ORDER - 1)]

    @staticmethod
    def vec_mul_scalar(data: bytes, scalar: int) -> bytes:
        """字节串逐字节乘以标量（秘密分享中的核心运算）。"""
        if scalar == 0:
            return b"\x00" * len(data)
        if scalar == 1:
            return bytes(data)
        log_s = _LOG[scalar]
        return bytes(_EXP[_LOG[b] + log_s] if b else 0 for b in data)

    @staticmethod
    def vec_xor(a: bytes, b: bytes) -> bytes:
        if len(a) != len(b):
            raise ParameterError("vector length mismatch")
        return bytes(x ^ y for x, y in zip(a, b))
