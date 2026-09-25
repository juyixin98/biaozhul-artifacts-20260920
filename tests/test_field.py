"""Field arithmetic tests over the secp256k1 prime field."""

import random

import pytest

from threshold_shares.field import (
    FIELD_PRIME,
    add,
    sub,
    mul,
    inverse,
    evaluate_polynomial,
    lagrange_interpolate_at_zero,
    FieldValueError,
)


def test_additive_and_multiplicative_identities():
    assert add(123, 0) == 123
    assert mul(123, 1) == 123
    assert sub(123, 123) == 0


def test_arithmetic_reduces_mod_p():
    assert add(FIELD_PRIME - 1, 5) == 4
    assert mul(FIELD_PRIME - 1, FIELD_PRIME - 1) == 1


def test_inverse_roundtrip_and_zero():
    rng = random.Random(0)
    for value in [1, 2, FIELD_PRIME - 1] + [rng.randrange(1, FIELD_PRIME) for _ in range(20)]:
        assert mul(value, inverse(value)) == 1
    with pytest.raises(FieldValueError):
        inverse(0)


def test_out_of_range_and_type_rejected():
    with pytest.raises(FieldValueError):
        add(-1, 1)
    with pytest.raises(FieldValueError):
        add(FIELD_PRIME, 0)
    with pytest.raises(FieldValueError):
        mul(True, 1)


def test_polynomial_constant_term_is_secret_at_zero():
    # f(x) = 42 + 7x + 9x^2
    coeffs = [42, 7, 9]
    # explicit evaluations
    assert evaluate_polynomial(coeffs, 1) == 58
    assert evaluate_polynomial(coeffs, 2) == (42 + 14 + 36)
    with pytest.raises(FieldValueError):
        evaluate_polynomial(coeffs, 0)


def test_lagrange_recovers_arbitrary_polynomial_at_zero():
    rng = random.Random(1)
    for degree in range(1, 6):
        coeffs = [rng.randrange(FIELD_PRIME) for _ in range(degree + 1)]
        xs = list(range(1, degree + 2))
        points = [(x, evaluate_polynomial(coeffs, x)) for x in xs]
        assert lagrange_interpolate_at_zero(points) == coeffs[0]


def test_lagrange_rejects_duplicate_and_zero_x():
    points = [(1, 10), (1, 20), (2, 30)]
    with pytest.raises(FieldValueError):
        lagrange_interpolate_at_zero(points)
    with pytest.raises(FieldValueError):
        lagrange_interpolate_at_zero([(0, 1), (2, 3)])
    with pytest.raises(FieldValueError):
        lagrange_interpolate_at_zero([(1, 1)])
