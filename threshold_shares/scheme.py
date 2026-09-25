"""Shamir secret sharing over GF(p) with versioned parameters.

Scheme SSS/1:
  * Field: secp256k1 prime field GF(p), p ~= 2^256 (see field.py).
  * The secret bytes are prefixed with a 4-byte big-endian length header and
    split into fixed CHUNK_SIZE-byte blocks. Every block is encoded as an
    integer and shared with an independent random degree-(threshold-1)
    polynomial. Independent polynomials per block keep the construction the
    standard textbook Shamir scheme rather than a custom variant.
  * Share i = (x=i, y_b for each block b), i in 1..total.
  * Recovery needs any `threshold` shares with distinct x values and runs
    Lagrange interpolation at x = 0.

Randomness comes from cryptography.hazmat backends via secrets (OS CSPRNG).
"""

from dataclasses import dataclass
import secrets

from . import field

PARAM_VERSION = 1
# A block must be smaller than p. 31 bytes (= 248 bits) safely fits under the
# 256-bit prime for every possible value, so no per-block range rejection is
# needed.
CHUNK_SIZE = 31
# All per-block arithmetic is kept inside the 248-bit block space. The field is
# GF(p) with p ~= 2^256, but encoding a block back to exactly CHUNK_SIZE bytes
# requires every y (and hence every interpolated constant term) to be < 2^248.
# Drawing polynomial coefficients from [0, 2^248) keeps the linear Shamir
# construction closed over that space (with x <= 255 small, evaluation stays
# well below p), so interpolation at zero recovers the exact block integer.
BLOCK_MOD = 1 << (8 * CHUNK_SIZE)
# x coordinates are stored in one byte of the versioned share format.
MAX_TOTAL_SHARES = 255
# The length header is 4 bytes big-endian, also encoded inside the shared data.
_LENGTH_PREFIX = 4


@dataclass(frozen=True)
class Share:
    """One share of a SSS/1 split."""

    x: int
    ys: tuple  # tuple[int, ...], one field element per block

    def __post_init__(self):
        if not isinstance(self.x, int) or isinstance(self.x, bool) or not 1 <= self.x <= MAX_TOTAL_SHARES:
            raise ValueError(f"share index x must be in 1..{MAX_TOTAL_SHARES}")
        if not self.ys or not all(isinstance(y, int) and 0 <= y < field.FIELD_PRIME for y in self.ys):
            raise ValueError("share ys must be a non-empty tuple of field elements")
        object.__setattr__(self, "ys", tuple(self.ys))


def _validate_parameters(threshold: int, total: int) -> None:
    if not isinstance(threshold, int) or isinstance(threshold, bool):
        raise ValueError("threshold must be an integer")
    if not isinstance(total, int) or isinstance(total, bool):
        raise ValueError("total must be an integer")
    if threshold < 2:
        raise ValueError("threshold must be at least 2")
    if total < threshold:
        raise ValueError("total must be >= threshold")
    if total > MAX_TOTAL_SHARES:
        raise ValueError(f"total must be <= {MAX_TOTAL_SHARES} (share format limit)")


def _encode_secret_blocks(secret: bytes) -> list:
    if not isinstance(secret, (bytes, bytearray)):
        raise ValueError("secret must be bytes")
    if len(secret) > 0xFFFFFFFF:
        raise ValueError("secret too long (4-byte length header)")
    framed = len(secret).to_bytes(_LENGTH_PREFIX, "big") + bytes(secret)
    # Zero-pad to a block boundary. On recovery every block is rendered as a
    # fixed CHUNK_SIZE-byte field element, so explicit zero padding makes the
    # byte layout round-trip exactly; the length header trims it back off.
    remainder = len(framed) % CHUNK_SIZE
    if remainder:
        framed += b"\x00" * (CHUNK_SIZE - remainder)
    blocks = [framed[i : i + CHUNK_SIZE] for i in range(0, len(framed), CHUNK_SIZE)]
    return [int.from_bytes(block, "big") for block in blocks]


def _decode_secret_blocks(values, expected_blocks: int) -> bytes:
    if len(values) != expected_blocks:
        raise ValueError(f"expected {expected_blocks} blocks, recovered {len(values)}")
    reconstructed_blocks = []
    for v in values:
        # A tampered (unauthenticated) share can make the interpolated value
        # leave the 248-bit block space; that surfaces here as OverflowError.
        try:
            reconstructed_blocks.append(v.to_bytes(CHUNK_SIZE, "big"))
        except OverflowError as exc:
            raise ValueError(
                "recovered field element is outside the block space; shares are inconsistent or corrupted"
            ) from exc
    reconstructed = b"".join(reconstructed_blocks)
    length = int.from_bytes(reconstructed[:_LENGTH_PREFIX], "big")
    payload = reconstructed[_LENGTH_PREFIX:]
    if length > len(payload):
        raise ValueError(
            f"recovered length header ({length} bytes) exceeds payload ({len(payload)} bytes); "
            "the shares are inconsistent or corrupted"
        )
    secret = payload[:length]
    # Everything beyond the declared length must be zero padding. This is a
    # structural sanity check only: without authenticated shares it proves
    # nothing about *which* share is bad.
    if any(b != 0 for b in payload[length:]):
        raise ValueError("non-zero data found past declared length; shares are inconsistent or corrupted")
    return secret


def split_secret(secret: bytes, threshold: int, total: int) -> list:
    """Split `secret` into `total` shares, any `threshold` of which recover it.

    Returns a list of Share. Raises ValueError for bad parameters.
    """
    _validate_parameters(threshold, total)
    blocks = _encode_secret_blocks(secret)

    # Independent degree-(threshold-1) polynomial per block:
    # c0 = block (the secret for that block), c1..c{k-1} random in the 248-bit
    # block space.
    polynomials = []
    for constant in blocks:
        coefficients = [constant] + [secrets.randbelow(BLOCK_MOD) for _ in range(threshold - 1)]
        polynomials.append(coefficients)

    shares = []
    for x in range(1, total + 1):
        ys = tuple(field.evaluate_polynomial(poly, x) for poly in polynomials)
        shares.append(Share(x=x, ys=ys))
    return shares


def recover_secret(shares, expected_blocks: int) -> bytes:
    """Recover the secret from at least `threshold` distinct-x Share objects.

    Caller is responsible for knowing `threshold`/block count; the service
    layer (service.py) carries the parameters on each share and enforces the
    threshold. Exactly `threshold` shares are used; extra shares are ignored.
    """
    if not shares:
        raise ValueError("no shares supplied")
    xs = [s.x for s in shares]
    if len(set(xs)) != len(xs):
        raise ValueError("duplicate share index (x coordinate)")
    if any(not isinstance(s, Share) for s in shares):
        raise ValueError("all inputs must be Share objects")

    values = []
    for block_index in range(expected_blocks):
        points = [(s.x, s.ys[block_index]) for s in shares]
        values.append(field.lagrange_interpolate_at_zero(points))
    return _decode_secret_blocks(values, expected_blocks)
