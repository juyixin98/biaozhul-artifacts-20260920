"""Pure-Python reference-model invariants (no chain involved)."""
import random

from app.reference import RefAccount, SCALE, int_to_limbs, limbs_to_int


def test_batch_equals_perblock_model():
    rng = random.Random(20260924)
    for _ in range(500):
        p = rng.randrange(0, 1 << 256)
        r = rng.randrange(0, SCALE + 1)
        n = rng.randrange(1, 200)

        batch = RefAccount(p, r, 0)
        batch.settle(n)

        per = RefAccount(p, r, 0)
        for b in range(1, n + 1):
            per.settle(b)

        assert (batch.fees, batch.carry) == (per.fees, per.carry)


def test_conservation_model():
    rng = random.Random(7)
    for _ in range(500):
        p = rng.randrange(0, 1 << 256)
        r = rng.randrange(0, SCALE + 1)
        n = rng.randrange(1, 500)
        a = RefAccount(p, r, 0)
        a.settle(n)
        # nothing lost, nothing created: fees*SCALE + carry == n*p*r exactly
        assert a.fees * SCALE + a.carry == n * p * r
        # rounding error strictly below one base unit
        assert 0 <= n * p * r - a.fees * SCALE < SCALE


def test_settle_same_block_is_noop():
    a = RefAccount(10**18, SCALE // 3, 0)
    a.settle(50)
    fees, carry = a.fees, a.carry
    assert a.settle(50) == 0  # no new blocks -> no new fees
    assert (a.fees, a.carry) == (fees, carry)


def test_rate_and_principal_changes_settle_first():
    a = RefAccount(1000, SCALE // 10, 0)
    a.settle(10)
    a.set_rate(SCALE // 5, at_block=10)
    a.settle(20)
    a.set_principal(5000, at_block=20)
    a.settle(35)

    b = RefAccount(1000, SCALE // 10, 0)
    for blk in range(1, 11):
        b.settle(blk)
    b.set_rate(SCALE // 5, at_block=10)
    for blk in range(11, 21):
        b.settle(blk)
    b.set_principal(5000, at_block=20)
    for blk in range(21, 36):
        b.settle(blk)

    assert (a.fees, a.carry) == (b.fees, b.carry)


def test_limbs_roundtrip():
    rng = random.Random(99)
    for _ in range(200):
        x = rng.randrange(0, 1 << 512)
        assert limbs_to_int(int_to_limbs(x)) == x
