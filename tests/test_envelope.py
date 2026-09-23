"""信封格式测试：往返、损坏检测、版本/域拒绝、HMAC 认证、重复横坐标（流程层）。"""

import base64
import unittest

from tss.envelope import (
    TSS1_MAGIC,
    FIELD_RIJNDAEL,
    FORMAT_VERSION,
    encode_share,
    decode_share,
    new_split_id,
    generate_auth_key,
)
from tss.errors import FormatError, IntegrityError, ParameterError


def _roundtrip_raw(raw_text, **kw):
    return decode_share(raw_text, **kw)


class TestEnvelopeRoundtrip(unittest.TestCase):
    def test_roundtrip_unauthenticated(self):
        sid = new_split_id()
        y = b"payload-bytes-\x00\xff"
        text = encode_share(3, 5, 7, sid, y)
        raw = decode_share(text)
        self.assertEqual(raw.threshold, 3)
        self.assertEqual(raw.total, 5)
        self.assertEqual(raw.x, 7)
        self.assertEqual(raw.split_id, sid)
        self.assertEqual(raw.y, y)
        self.assertFalse(raw.authenticated)

    def test_roundtrip_authenticated(self):
        key = generate_auth_key()
        sid = new_split_id()
        y = b"auth payload"
        text = encode_share(2, 3, 1, sid, y, auth_key=key)
        raw = decode_share(text, auth_key=key)
        self.assertTrue(raw.authenticated)
        self.assertEqual(raw.y, y)

    def test_text_is_standard_base64(self):
        text = encode_share(2, 3, 1, new_split_id(), b"x")
        # 必须能被严格模式解码，且只含标准字母表（不含 URL-safe 的 - 与 _）
        base64.b64decode(text, validate=True)
        self.assertNotIn("-", text)
        self.assertNotIn("_", text)


class TestEnvelopeRejection(unittest.TestCase):
    def setUp(self):
        self.sid = new_split_id()
        self.text = encode_share(3, 5, 2, self.sid, b"hello world")

    def test_bad_base64(self):
        with self.assertRaises(FormatError):
            decode_share("not base64!!!")
        with self.assertRaises(FormatError):
            decode_share("aGVsbG8")  # 非法 padding 长度

    def test_truncated(self):
        with self.assertRaises(FormatError):
            decode_share(base64.standard_b64encode(b"TSS1").decode())

    def test_bad_magic(self):
        raw = bytearray(base64.standard_b64decode(self.text))
        raw[0:4] = b"XXXX"
        with self.assertRaises(FormatError):
            decode_share(base64.standard_b64encode(bytes(raw)).decode())

    def test_unknown_version_rejected(self):
        raw = bytearray(base64.standard_b64decode(self.text))
        raw[4] = FORMAT_VERSION + 1
        with self.assertRaises(FormatError):
            decode_share(base64.standard_b64encode(bytes(raw)).decode())

    def test_unknown_field_rejected(self):
        raw = bytearray(base64.standard_b64decode(self.text))
        raw[5] = 0x77
        with self.assertRaises(FormatError):
            decode_share(base64.standard_b64encode(bytes(raw)).decode())

    def test_reserved_flag_rejected(self):
        raw = bytearray(base64.standard_b64decode(self.text))
        raw[6] = 0x80
        with self.assertRaises(FormatError):
            decode_share(base64.standard_b64encode(bytes(raw)).decode())

    def test_bad_params_rejected(self):
        raw = bytearray(base64.standard_b64decode(self.text))
        raw[7] = 1  # threshold < 2
        with self.assertRaises(FormatError):
            decode_share(base64.standard_b64encode(bytes(raw)).decode())

    def test_length_field_tampering(self):
        raw = bytearray(base64.standard_b64decode(self.text))
        raw[29] = raw[29] ^ 0x01  # 篡改载荷长度最低字节
        with self.assertRaises(FormatError):
            decode_share(base64.standard_b64encode(bytes(raw)).decode())


class TestIntegrity(unittest.TestCase):
    def test_checksum_detects_flip(self):
        text = encode_share(2, 3, 1, new_split_id(), b"abc")
        raw = bytearray(base64.standard_b64decode(text))
        raw[30] ^= 0x01
        flipped = base64.standard_b64encode(bytes(raw)).decode()
        with self.assertRaises(IntegrityError):
            decode_share(flipped)

    def test_checksum_detects_tag_flip(self):
        text = encode_share(2, 3, 1, new_split_id(), b"abc")
        raw = bytearray(base64.standard_b64decode(text))
        raw[-1] ^= 0x80
        with self.assertRaises(IntegrityError):
            decode_share(base64.standard_b64encode(bytes(raw)).decode())

    def test_checksum_does_not_stop_recomputing_attack(self):
        # 安全模型说明：无认证时，攻击者可改载荷后重算 SHA256 标签，
        # 校验和无法识别这种恶意伪造（只能防意外损坏）。
        import hashlib
        text = encode_share(2, 3, 1, new_split_id(), b"abc")
        raw = bytearray(base64.standard_b64decode(text))
        raw[30:33] = b"XYZ"
        raw[-32:] = hashlib.sha256(bytes(raw[:-32])).digest()
        decoded = decode_share(base64.standard_b64encode(bytes(raw)).decode())
        self.assertEqual(decoded.y, b"XYZ")  # 伪造通过 —— 因此需要 HMAC 模式

    def test_hmac_rejects_tampering(self):
        key = generate_auth_key()
        text = encode_share(2, 3, 1, new_split_id(), b"secret", auth_key=key)
        raw = bytearray(base64.standard_b64decode(text))
        raw[30] ^= 0x01
        with self.assertRaises(IntegrityError):
            decode_share(base64.standard_b64encode(bytes(raw)).decode(), auth_key=key)

    def test_hmac_rejects_recomputed_sha_attack(self):
        # 攻击者不知道 HMAC 密钥：即便重算 SHA256 也无法通过
        import hashlib
        key = generate_auth_key()
        text = encode_share(2, 3, 1, new_split_id(), b"secret", auth_key=key)
        raw = bytearray(base64.standard_b64decode(text))
        raw[30:36] = b"forged"
        raw[-32:] = hashlib.sha256(bytes(raw[:-32])).digest()
        with self.assertRaises(IntegrityError):
            decode_share(base64.standard_b64encode(bytes(raw)).decode(), auth_key=key)

    def test_hmac_wrong_key(self):
        key = generate_auth_key()
        other = generate_auth_key()
        text = encode_share(2, 3, 1, new_split_id(), b"secret", auth_key=key)
        with self.assertRaises(IntegrityError):
            decode_share(text, auth_key=other)

    def test_authenticated_share_without_key_fail_closed(self):
        key = generate_auth_key()
        text = encode_share(2, 3, 1, new_split_id(), b"secret", auth_key=key)
        # 不提供密钥直接当普通信封解：标签算法不匹配 -> IntegrityError
        with self.assertRaises(IntegrityError):
            decode_share(text)

    def test_short_auth_key_rejected(self):
        sid = new_split_id()
        with self.assertRaises(ParameterError):
            encode_share(2, 3, 1, sid, b"x", auth_key=b"short")
        with self.assertRaises(ParameterError):
            generate_auth_key(length=8)


if __name__ == "__main__":
    unittest.main()
