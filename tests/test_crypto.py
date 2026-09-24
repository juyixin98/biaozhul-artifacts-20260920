"""Unit tests for crypto primitives."""

import hashlib

import pytest

from segtask_server import crypto


def test_secret_is_32_random_bytes(secret):
    assert len(secret) == 32
    assert secret != crypto.generate_secret()


def test_chain_link_deterministic_and_order_sensitive(secret):
    a = crypto.chain_hash(secret, crypto.GENESIS, {'x': 1})
    b = crypto.chain_hash(secret, crypto.GENESIS, {'x': 1})
    assert a == b
    assert crypto.verify_chain_link(secret, crypto.GENESIS, {'x': 1}, a)
    assert a != crypto.chain_hash(secret, crypto.GENESIS, {'x': 2})
    assert a != crypto.chain_hash(secret, '0' * 64, {'x': 1})
    # Tampering with the key breaks verification (constant-time compare).
    assert not crypto.verify_chain_link(b'X' * 32, crypto.GENESIS, {'x': 1}, a)


def test_canonical_payload_key_order_independent():
    assert crypto.canonical_payload({'a': 1, 'b': 2}) == \
        crypto.canonical_payload({'b': 2, 'a': 1})


def test_compute_segment_is_real_sha256_chained_work():
    seed = '00' * 32
    digest = bytes.fromhex(seed)
    for _ in range(1000):
        digest = hashlib.sha256(digest).digest()
    out, canceled = crypto.compute_segment(seed, 1000)
    assert not canceled
    assert out == digest.hex()
    # Resume reproduces identical output.
    out2, _ = crypto.compute_segment(seed, 1000)
    assert out2 == out


def test_compute_segment_cancel_checkpoint_observed():
    calls = {'n': 0}

    def cancel():
        calls['n'] += 1
        return True  # cancel at the very first checkpoint

    out, canceled = crypto.compute_segment('00' * 32, 10_000_000,
                                           should_cancel=cancel,
                                           checkpoint_every=1000)
    assert canceled
    assert calls['n'] == 1


def test_secret_file_permissions(tmp_path):
    path = str(tmp_path / 'chain.key')
    s1 = crypto.load_or_create_secret(path)
    import os
    import stat
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    s2 = crypto.load_or_create_secret(path)
    assert s1 == s2


def test_secret_file_rejects_loose_permissions(tmp_path):
    path = str(tmp_path / 'loose.key')
    with open(path, 'wb') as fh:
        fh.write(b'x' * 32)
    import os
    os.chmod(path, 0o644)
    with pytest.raises(PermissionError):
        crypto.load_or_create_secret(path)
