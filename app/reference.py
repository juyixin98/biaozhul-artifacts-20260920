"""Pure-Python reference model of the on-chain settlement math.

Mirrors contracts/FeeSettlement.sol exactly, using Python big integers.
Used by the test-suite to independently verify on-chain results, and by
scripts/demo.py to print the rounding-error bound.

Model (SCALE = 2**64):
    T     = carry + n * principal * rate
    fees += T // SCALE
    carry  = T %  SCALE
"""

SCALE = 1 << 64
MAX_RATE = SCALE
MAX_UINT256 = (1 << 256) - 1


class RefAccount:
    def __init__(self, principal: int, rate: int, start_block: int):
        if not 0 <= rate <= MAX_RATE:
            raise ValueError("rate out of range")
        if not 0 <= principal <= MAX_UINT256:
            raise ValueError("principal out of range")
        self.principal = principal
        self.rate = rate
        self.fees = 0
        self.carry = 0
        self.last_block = start_block

    def settle(self, to_block: int) -> int:
        """Settle up to `to_block`; returns the fee delta of this settlement."""
        n = to_block - self.last_block
        if n <= 0:
            return 0
        total = self.carry + n * self.principal * self.rate
        q, r = divmod(total, SCALE)
        self.fees += q
        self.carry = r
        self.last_block = to_block
        return q

    def set_rate(self, new_rate: int, at_block: int) -> int:
        delta = self.settle(at_block)
        if not 0 <= new_rate <= MAX_RATE:
            raise ValueError("rate out of range")
        self.rate = new_rate
        return delta

    def set_principal(self, new_principal: int, at_block: int) -> int:
        delta = self.settle(at_block)
        if not 0 <= new_principal <= MAX_UINT256:
            raise ValueError("principal out of range")
        self.principal = new_principal
        return delta


def limbs_to_int(limbs) -> int:
    """U512 limbs (base 2**128, little-endian) -> Python int."""
    return sum(int(l) << (128 * i) for i, l in enumerate(limbs))


def int_to_limbs(x: int):
    """Python int -> 4 U512 limbs; raises if x >= 2**512."""
    if x < 0 or x >= 1 << 512:
        raise ValueError("does not fit in 512 bits")
    mask = (1 << 128) - 1
    return [x & mask, (x >> 128) & mask, (x >> 256) & mask, (x >> 384) & mask]
