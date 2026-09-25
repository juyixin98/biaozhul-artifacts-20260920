"""Store-level tests: ranges, empty object, short final block, ciphertext
swaps and tamper resistance. These carry the acceptance criteria.
"""

import os
import struct

import pytest

from enrange.errors import (
    AuthenticationError,
    InvalidRangeError,
    NotFoundError,
)
from enrange.store import HEADER_SIZE, ObjectStore

OID = "00" * 16  # fixed id for tamper tests


# --------------------------------------------------------------------- basics


def test_put_and_get_full_roundtrip(store):
    payload = bytes(range(256)) * 10  # 2560 bytes
    meta = store.put(OID, payload, block_size=64)
    assert meta == {"id": OID, "length": 2560, "block_size": 64, "blocks": 40}
    assert store.get(OID) == payload


def test_empty_object(store):
    meta = store.put(OID, b"", block_size=64)
    assert meta["length"] == 0
    assert meta["blocks"] == 0
    assert store.get(OID) == b""
    assert store.read_range(OID, 0, 0) == b""
    assert store.read_range(OID, 0, None) == b""
    info = store.stat(OID)
    assert info["length"] == 0 and info["blocks"] == 0
    # Only the header exists on disk; encapsulated region is empty.
    assert os.path.getsize(store._path(OID)) == HEADER_SIZE


# --------------------------------------------------------- range boundaries


def test_first_and_last_byte_ranges(store):
    # 3.5 blocks so the final block is short: block_size 10, len 35
    payload = bytes((i % 251) + 1 for i in range(35))
    store.put(OID, payload, block_size=10)

    assert store.read_range(OID, 0, 1) == payload[:1]          # first byte
    assert store.read_range(OID, 34, 35) == payload[-1:]       # last byte
    assert store.read_range(OID, 0, 10) == payload[:10]        # first block
    assert store.read_range(OID, 30, 35) == payload[30:35]     # short last block
    assert store.read_range(OID, 5, 30) == payload[5:30]       # cross-block
    assert store.read_range(OID, 8, 22) == payload[8:22]       # crosses boundary
    assert store.read_range(OID, 0, None) == payload           # open end
    assert store.read_range(OID, 0, 35) == payload             # full as range
    assert store.read_range(OID, 10, 10) == b""                # empty inside


@pytest.mark.parametrize(
    "start,end",
    [(-1, 1), (0, 36), (35, 34), (20, 10), (36, 40)],
)
def test_invalid_ranges_rejected(store, start, end):
    store.put(OID, b"x" * 35, block_size=10)
    with pytest.raises(InvalidRangeError):
        store.read_range(OID, start, end)


def test_get_missing_object(store):
    with pytest.raises(NotFoundError):
        store.get(OID)


def test_object_id_path_traversal_rejected(store):
    with pytest.raises(ValueError):
        store.get("../../etc/passwd")


# -------------------------------------------------------- final short block


def test_last_block_short_but_authenticated(store):
    payload = b"A" * 10 + b"B" * 10 + b"C" * 3  # 23, block 10
    store.put(OID, payload, block_size=10)
    assert store.read_range(OID, 20, 23) == b"CCC"
    # The short block is genuinely 3 bytes (plus tag), not a padded full block.
    file_size = os.path.getsize(store._path(OID))
    # 2 full frames (10+16)*2 + 1 short frame (3+16) + header
    assert file_size == HEADER_SIZE + 2 * 26 + 19


def test_tampering_with_short_last_block(store):
    payload = b"A" * 10 + b"B" * 10 + b"C" * 3
    store.put(OID, payload, block_size=10)
    path = store._path(OID)
    raw = bytearray(path.read_bytes())
    # Flip a byte inside the last (short) frame's ciphertext.
    raw[-1] ^= 0x01
    path.write_bytes(raw)
    with pytest.raises(AuthenticationError):
        store.read_range(OID, 20, 23)
    with pytest.raises(AuthenticationError):
        store.get(OID)


# -------------------------------------------------------- ciphertext swaps


def test_swap_two_blocks_within_one_object(store):
    # 4 blocks; swap block 0 and block 1 frames (identical length, 26 bytes).
    payload = b"".join(bytes([c]) * 10 for c in b"ABCD")
    store.put(OID, payload, block_size=10)

    path = store._path(OID)
    raw = bytearray(path.read_bytes())
    frame = 10 + 16
    block0 = raw[HEADER_SIZE : HEADER_SIZE + frame]
    block1 = raw[HEADER_SIZE + frame : HEADER_SIZE + 2 * frame]
    raw[HEADER_SIZE : HEADER_SIZE + frame] = block1
    raw[HEADER_SIZE + frame : HEADER_SIZE + 2 * frame] = block0
    path.write_bytes(raw)

    # Reading block 0 must fail authentication: AAD pins block index/offset.
    with pytest.raises(AuthenticationError):
        store.read_range(OID, 0, 10)
    with pytest.raises(AuthenticationError):
        store.get(OID)


def test_swap_block_across_different_objects(store):
    a = b"".join(bytes([c]) * 10 for c in b"AAAA")
    b = b"".join(bytes([c]) * 10 for c in b"BBBB")
    id_a = "11" * 16
    id_b = "22" * 16
    store.put(id_a, a, block_size=10)
    store.put(id_b, b, block_size=10)

    path_a, path_b = store._path(id_a), store._path(id_b)
    ra, rb = bytearray(path_a.read_bytes()), bytearray(path_b.read_bytes())
    frame = 26
    # Copy block 2 frame of B into block 2 slot of A: different per-object key
    # and different object id in AAD, so it must fail.
    ra[HEADER_SIZE + 2 * frame : HEADER_SIZE + 3 * frame] = rb[
        HEADER_SIZE + 2 * frame : HEADER_SIZE + 3 * frame
    ]
    path_a.write_bytes(ra)
    with pytest.raises(AuthenticationError):
        store.read_range(id_a, 20, 30)
    with pytest.raises(AuthenticationError):
        store.get(id_a)
    # B itself is untouched.
    assert store.get(id_b) == b


def test_move_block_to_different_position_same_length(store):
    # Same object, moving a frame two slots away (same length) must fail.
    payload = b"".join(bytes([c]) * 10 for c in b"WXYZ")
    store.put(OID, payload, block_size=10)
    path = store._path(OID)
    raw = bytearray(path.read_bytes())
    frame = 26
    src = raw[HEADER_SIZE + 3 * frame : HEADER_SIZE + 4 * frame]
    raw[HEADER_SIZE : HEADER_SIZE + frame] = src
    path.write_bytes(raw)
    with pytest.raises(AuthenticationError):
        store.read_range(OID, 0, 5)


# ------------------------------------------------------------- tamper tests


def test_flip_bit_in_first_block_ciphertext(store):
    payload = b"".join(bytes([c]) * 10 for c in b"ABCD")
    store.put(OID, payload, block_size=10)
    path = store._path(OID)
    raw = bytearray(path.read_bytes())
    raw[HEADER_SIZE + 3] ^= 0x80  # inside block 0 ciphertext
    path.write_bytes(raw)

    # Whole-object read fails.
    with pytest.raises(AuthenticationError):
        store.get(OID)
    # A range confined to the untouched block 3 still triggers a header-only
    # read of that block, which verifies.
    assert store.read_range(OID, 30, 40) == payload[30:40]
    # But any range touching block 0 fails.
    with pytest.raises(AuthenticationError):
        store.read_range(OID, 0, 40)
    with pytest.raises(AuthenticationError):
        store.read_range(OID, 5, 15)


def test_tampered_tag_only(store):
    store.put(OID, b"hello world" * 3, block_size=16)
    path = store._path(OID)
    raw = bytearray(path.read_bytes())
    raw[HEADER_SIZE + 16 + 10] ^= 0xFF  # inside first frame's GCM tag
    path.write_bytes(raw)
    with pytest.raises(AuthenticationError):
        store.get(OID)


def test_tamper_header_plaintext_length(store):
    store.put(OID, b"x" * 25, block_size=10)
    path = store._path(OID)
    raw = bytearray(path.read_bytes())
    # plaintext_len uint64 lives at bytes 13..20; change 25 -> 24
    struct.pack_into(">Q", raw, 13, 24)
    path.write_bytes(raw)
    with pytest.raises(AuthenticationError):
        store.stat(OID)
    with pytest.raises(AuthenticationError):
        store.get(OID)


def test_tamper_header_block_size(store):
    store.put(OID, b"x" * 25, block_size=10)
    path = store._path(OID)
    raw = bytearray(path.read_bytes())
    struct.pack_into(">I", raw, 9, 11)
    path.write_bytes(raw)
    with pytest.raises(AuthenticationError):
        store.get(OID)


def test_truncated_file_detected(store):
    store.put(OID, b"x" * 25, block_size=10)
    path = store._path(OID)
    raw = path.read_bytes()
    path.write_bytes(raw[:-1])  # chop one byte off last frame's tag
    with pytest.raises(AuthenticationError):
        store.get(OID)


def test_appended_bytes_detected(store):
    store.put(OID, b"x" * 25, block_size=10)
    path = store._path(OID)
    path.write_bytes(path.read_bytes() + b"\x00")
    with pytest.raises(AuthenticationError):
        store.get(OID)


def test_truncated_header_detected(store):
    store.put(OID, b"x" * 25, block_size=10)
    path = store._path(OID)
    path.write_bytes(path.read_bytes()[:20])
    with pytest.raises(AuthenticationError):
        store.get(OID)


def test_bad_magic_detected(store):
    store.put(OID, b"x" * 10, block_size=10)
    path = store._path(OID)
    raw = bytearray(path.read_bytes())
    raw[0] = ord(b"X")
    path.write_bytes(raw)
    with pytest.raises(AuthenticationError):
        store.get(OID)


def test_wrong_master_key_fails(tmp_path):
    d1, d2 = tmp_path / "a", tmp_path / "b"
    s1 = ObjectStore.open_or_create(d1)
    s1.put(OID, b"secret", block_size=4)
    # A second store with its own random key cannot authenticate the object,
    # even after copying the file into its objects dir.
    s2 = ObjectStore.open_or_create(d2)
    (d2 / "objects" / f"{OID}.bin").write_bytes((d1 / "objects" / f"{OID}.bin").read_bytes())
    with pytest.raises(AuthenticationError):
        s2.get(OID)


# ------------------------------------------------------------- key hygiene


def test_key_file_is_0600(data_dir):
    store = ObjectStore.open_or_create(data_dir)
    store.put(OID, b"x")
    mode = (data_dir / "master.key").stat().st_mode & 0o777
    assert mode == 0o600


def test_key_is_stable_across_open(data_dir):
    s1 = ObjectStore.open_or_create(data_dir)
    s2 = ObjectStore.open_or_create(data_dir)
    assert s1.master_key == s2.master_key
