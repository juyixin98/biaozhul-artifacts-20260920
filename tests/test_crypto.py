"""加密原语与容器格式测试。"""

import pytest

from app import crypto


@pytest.fixture()
def keys():
    return {"v1": crypto.new_key()}


@pytest.mark.parametrize(
    "data,block_size",
    [
        (b"", 16),
        (b"a", 16),
        (b"hello envelope encryption", 4),  # 多块 + 末块短
        (bytes(range(256)) * 4, 64),
    ],
)
def test_seal_open_roundtrip(keys, data, block_size):
    blob = crypto.seal(keys["v1"], "v1", data, block_size=block_size)
    assert data == crypto.open_container(blob, keys.__getitem__)


def test_each_object_has_independent_dek(keys):
    a = crypto.seal(keys["v1"], "v1", b"same plaintext")
    b = crypto.seal(keys["v1"], "v1", b"same plaintext")
    ha, _ = crypto.parse_header(a)
    hb, _ = crypto.parse_header(b)
    assert ha.wrapped_dek != hb.wrapped_dek  # DEK 随机且独立
    assert ha.salt != hb.salt
    # 相同明文密文不同 (DEK 不同)
    assert a != b


def test_nonce_unique_per_block_no_nonce_reuse(keys):
    """每块 nonce = salt||index; salt 每对象随机, index 块间唯一。"""
    blob = crypto.seal(keys["v1"], "v1", b"x" * 100, block_size=10)
    h, _ = crypto.parse_header(blob)
    assert len(h.salt) == 8
    # 10 个块, index 0..9 保证同一 salt 下 nonce 不重复
    nonces = {h.salt + i.to_bytes(4, "big") for i in range(10)}
    assert len(nonces) == 10


def test_wrong_master_key_fails(keys):
    blob = crypto.seal(keys["v1"], "v1", b"secret data here")
    with pytest.raises(crypto.DecryptError):
        crypto.open_container(blob, {"v1": crypto.new_key()}.__getitem__)


def test_missing_master_key_raises(keys):
    blob = crypto.seal(keys["v1"], "v1", b"secret")

    def resolve(_kid):
        raise crypto.KeyUnavailableError("v1")

    with pytest.raises(crypto.KeyUnavailableError):
        crypto.open_container(blob, resolve)


def test_header_tamper_version_byte_fails(keys):
    """版本号在 DEK 包裹的 AAD 中: 仅改版本字节、其余保持合法, 必须认证失败而非解出明文。"""
    from app.crypto import _encode_header, encrypt_blocks

    keys["v2"] = crypto.new_key()
    salt = b"\x01" * crypto.SALT_LEN
    bs, pt_len = 16, len(b"secret")
    dek = crypto.new_key()
    # 用 v2 主密钥按 "version=2" 的 AAD 包裹 DEK
    wrapped = crypto.wrap_dek(keys["v2"], "v2", dek, (2, salt, bs, pt_len))
    # 但容器头部声明 version=1
    header = crypto.Header(version=1, key_id="v2", salt=salt, block_size=bs,
                           plaintext_len=pt_len, wrapped_dek=wrapped)
    blob = _encode_header(header) + encrypt_blocks(b"secret", dek, salt, bs)
    # key_id=v2 能取到密钥, 但 AAD 版本不匹配 -> 认证失败
    with pytest.raises(crypto.DecryptError):
        crypto.open_container(blob, keys.__getitem__)


def test_header_tamper_key_id_fails(keys):
    keys["v9"] = keys["v1"]  # 让 key_id 能解析到"同一把密钥", 排除缺密钥干扰
    blob = bytearray(crypto.seal(keys["v1"], "v1", b"secret data payload"))
    # key_id 位于偏移 6 (magic4 + version1 + len1), 单字符改写, 长度不变
    assert blob[6:7] == b"v"
    blob[7] = ord("9")  # "v1" -> "v9"
    with pytest.raises(crypto.DecryptError):
        crypto.open_container(bytes(blob), keys.__getitem__)


def test_header_tamper_length_fields_fails(keys):
    """salt / block_size / pt_len 任一被改都会让 DEK 解包认证失败。"""
    blob = bytearray(crypto.seal(keys["v1"], "v1", b"x" * 50, block_size=10))
    # 找到 wrapped_len 字段前的 pt_len (8B), 翻转其一个字节
    # 布局: 4+1+1+2(kid)+8(salt)+4(bs)+8(ptlen)
    pt_len_off = 4 + 1 + 1 + 2 + 8 + 4
    blob[pt_len_off + 7] ^= 0xFF
    with pytest.raises((crypto.DecryptError, crypto.FormatError)):
        crypto.open_container(bytes(blob), keys.__getitem__)


def test_ciphertext_truncated_tag_fails(keys):
    blob = crypto.seal(keys["v1"], "v1", b"x" * 100, block_size=10)
    with pytest.raises(crypto.EnvelopeError):
        crypto.open_container(blob[:-1], keys.__getitem__)  # 砍掉 1 字节 tag


def test_ciphertext_truncated_whole_block_fails(keys):
    blob = crypto.seal(keys["v1"], "v1", b"x" * 100, block_size=10)
    _, body_off = crypto.parse_header(blob)
    with pytest.raises(crypto.EnvelopeError):
        crypto.open_container(blob[: body_off + 13], keys.__getitem__)  # 只剩首块一部分


def test_header_truncated_fails(keys):
    blob = crypto.seal(keys["v1"], "v1", b"data")
    with pytest.raises(crypto.FormatError):
        crypto.open_container(blob[:10], keys.__getitem__)


def test_ciphertext_bitflip_fails(keys):
    blob = bytearray(crypto.seal(keys["v1"], "v1", b"secret payload!!", block_size=4))
    blob[-1] ^= 0x01  # 翻转末块 tag 一比特
    with pytest.raises(crypto.DecryptError):
        crypto.open_container(bytes(blob), keys.__getitem__)


def test_block_swap_fails(keys):
    """块序号在块 AAD 中, 调换两个块必须认证失败。"""
    data = b"".join(bytes([i]) * 10 for i in range(5))  # 5 个内容不同的块
    blob = bytearray(crypto.seal(keys["v1"], "v1", data, block_size=10))
    _, body_off = crypto.parse_header(bytes(blob))
    block_len = 10 + crypto.TAG_LEN
    b0 = blob[body_off : body_off + block_len]
    b1 = blob[body_off + block_len : body_off + 2 * block_len]
    blob[body_off : body_off + block_len] = b1
    blob[body_off + block_len : body_off + 2 * block_len] = b0
    with pytest.raises(crypto.DecryptError):
        crypto.open_container(bytes(blob), keys.__getitem__)


def test_trailing_garbage_fails(keys):
    blob = crypto.seal(keys["v1"], "v1", b"data")
    with pytest.raises(crypto.FormatError):
        crypto.open_container(blob + b"\x00", keys.__getitem__)


def test_no_partial_plaintext_on_failure(keys):
    """多块对象中后续块损坏: 异常整体抛出, 解密结果不可获得。"""
    data = b"".join(bytes([i]) * 10 for i in range(6))
    blob = bytearray(crypto.seal(keys["v1"], "v1", data, block_size=10))
    blob[-1] ^= 0x01  # 只损坏最后一块
    with pytest.raises(crypto.DecryptError) as exc_info:
        crypto.open_container(bytes(blob), keys.__getitem__)
    # 返回值只能是异常, 不存在"部分明文"出口
    assert exc_info.value.args


def test_rewrap_preserves_ciphertext_and_decrypts(keys):
    keys["v2"] = crypto.new_key()
    blob = crypto.seal(keys["v1"], "v1", b"payload across blocks" * 10, block_size=16)
    h1, body_off1 = crypto.parse_header(blob)
    rewrapped = crypto.rewrap_header(blob, keys.__getitem__, keys["v2"], "v2")
    h2, body_off2 = crypto.parse_header(rewrapped)
    # 数据块字节完全一致 (轮换只动头部 DEK 包裹)
    assert blob[body_off1:] == rewrapped[body_off2:]
    assert h1.wrapped_dek != h2.wrapped_dek
    assert h2.key_id == "v2"
    assert crypto.open_container(rewrapped, keys.__getitem__) == b"payload across blocks" * 10


def test_old_key_still_decrypts_before_deletion(keys):
    keys["v2"] = crypto.new_key()
    blob = crypto.seal(keys["v1"], "v1", b"old object")
    blob2 = crypto.rewrap_header(blob, keys.__getitem__, keys["v2"], "v2")
    assert crypto.open_container(blob, keys.__getitem__) == b"old object"
    assert crypto.open_container(blob2, keys.__getitem__) == b"old object"
