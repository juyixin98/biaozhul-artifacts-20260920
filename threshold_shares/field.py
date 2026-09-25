"""Prime-field arithmetic for Shamir secret sharing.

We work in GF(p) where p is the well-known secp256k1 base-field prime
(the same prime used by the secp256k1 elliptic curve, e.g. Bitcoin/Ethereum).
Only the prime field itself is used here -- no elliptic-curve operations and
no home-grown cryptography: addition/multiplication reduce modulo a published,
standard modulus, and the multiplicative inverse uses Python's standard
modular exponentiation (Fermat's little theorem, p prime).
"""

# secp256k1 field prime p = 2^256 - 2^32 - 977
FIELD_PRIME = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
FIELD_NAME = "secp256k1-prime"


class FieldValueError(ValueError):
    """An operand is not a valid element of GF(p)."""


def _as_field_element(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise FieldValueError(f"field element must be an integer, got {type(value).__name__}")
    if not 0 <= value < FIELD_PRIME:
        raise FieldValueError("field element out of range [0, p-1]")
    return value


def add(a: int, b: int) -> int:
    return (_as_field_element(a) + _as_field_element(b)) % FIELD_PRIME


def sub(a: int, b: int) -> int:
    return (_as_field_element(a) - _as_field_element(b)) % FIELD_PRIME


def mul(a: int, b: int) -> int:
    return (_as_field_element(a) * _as_field_element(b)) % FIELD_PRIME


def inverse(a: int) -> int:
    """Multiplicative inverse of a in GF(p); 0 has no inverse."""
    a = _as_field_element(a)
    if a == 0:
        raise FieldValueError("zero has no multiplicative inverse")
    return pow(a, FIELD_PRIME - 2, FIELD_PRIME)


def evaluate_polynomial(coefficients, x: int) -> int:
    """Evaluate polynomial given low-degree-first coefficients at x (Horner).

    y = c0 + c1*x + c2*x^2 + ...  (mod p)
    """
    x = _as_field_element(x)
    if x == 0:
        # x=0 is reserved (it is the secret position); shares never use it.
        raise FieldValueError("polynomial evaluation at x=0 is reserved for the secret")
    result = 0
    for coefficient in reversed(list(coefficients)):
        result = (result * x + _as_field_element(coefficient)) % FIELD_PRIME
    return result


def lagrange_interpolate_at_zero(points) -> int:
    """Lagrange interpolation of f(0) from distinct points (x_i, y_i) in GF(p).

    All x_i must be distinct and non-zero. Standard textbook Lagrange basis
    evaluated at x = 0:

        L_i(0) = prod_{j != i} (0 - x_j) / (x_i - x_j)
        f(0)   = sum_i y_i * L_i(0)   (all arithmetic in GF(p))
    """
    points = [( _as_field_element(x), _as_field_element(y)) for x, y in points]
    if len(points) < 2:
        raise FieldValueError("at least two points are required for interpolation")
    seen_x = set()
    for x, _ in points:
        if x == 0:
            raise FieldValueError("x=0 is reserved for the secret")
        if x in seen_x:
            raise FieldValueError("duplicate x coordinate in interpolation input")
        seen_x.add(x)

    total = 0
    for i, (x_i, y_i) in enumerate(points):
        numerator = 1
        denominator = 1
        for j, (x_j, _) in enumerate(points):
            if i == j:
                continue
            numerator = (numerator * (-x_j)) % FIELD_PRIME
            denominator = (denominator * (x_i - x_j)) % FIELD_PRIME
        term = (y_i * numerator * pow(denominator, FIELD_PRIME - 2, FIELD_PRIME)) % FIELD_PRIME
        total = (total + term) % FIELD_PRIME
    return total
