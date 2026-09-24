import numpy as np

from app.transforms import apply, se2, se2_inv


def test_se2_inverse_roundtrip():
    t = se2(1.5, -2.0, 0.7)
    assert np.allclose(t @ se2_inv(t), np.eye(3), atol=1e-12)
    assert np.allclose(se2_inv(t) @ t, np.eye(3), atol=1e-12)


def test_apply_matches_manual_computation():
    theta = np.pi / 2
    t = se2(1.0, 2.0, theta)
    pts = np.array([[1.0, 0.0], [0.0, 1.0]])
    out = apply(t, pts)
    # R(90deg) @ [1,0] + [1,2] = [1, 3]; R(90deg) @ [0,1] + [1,2] = [0, 2]
    assert np.allclose(out, [[1.0, 3.0], [0.0, 2.0]], atol=1e-12)


def test_composition_order():
    # A @ B applies B first.
    a = se2(1.0, 0.0, 0.0)
    b = se2(0.0, 1.0, 0.0)
    out = apply(a @ b, np.array([[0.0, 0.0]]))
    assert np.allclose(out, [[1.0, 1.0]], atol=1e-12)
