"""A tiny, dependency-free model used to validate the switching machinery.

The model is a 1-hidden-layer perceptron implemented directly on NumPy.  It is
deliberately *not* trained against downloaded data: every version is built
synthetically (see :func:`build_version_weights`) so that the *exact* warm-up
output of each version is known in advance and can be asserted byte-for-byte.
"""

from __future__ import annotations

import numpy as np

INPUT_DIM = 4
HIDDEN_DIM = 8
OUTPUT_DIM = 2

# Layer names persisted inside an artifact, in load order.
LAYER_NAMES = ("W1", "b1", "W2", "b2")
WEIGHT_DTYPES = {
    "W1": (np.float32, (INPUT_DIM, HIDDEN_DIM)),
    "b1": (np.float32, (HIDDEN_DIM,)),
    "W2": (np.float32, (HIDDEN_DIM, OUTPUT_DIM)),
    "b2": (np.float32, (OUTPUT_DIM,)),
}


def build_version_weights(version: int) -> dict[str, np.ndarray]:
    """Build deterministic, per-version weights without any randomness.

    Each version gets distinct values (so predictions differ between
    versions, which lets tests prove in-flight requests keep using the old
    weights) but the construction is fully reproducible.
    """
    if version < 1:
        raise ValueError("version must be a positive integer")
    rng = np.random.default_rng(1000 + version)
    scale = 1.0 / np.sqrt(INPUT_DIM)
    return {
        "W1": (rng.standard_normal((INPUT_DIM, HIDDEN_DIM)) * scale).astype(np.float32),
        "b1": (rng.standard_normal(HIDDEN_DIM) * 0.1).astype(np.float32),
        "W2": (rng.standard_normal((HIDDEN_DIM, OUTPUT_DIM)) / np.sqrt(HIDDEN_DIM)).astype(
            np.float32
        ),
        "b2": (rng.standard_normal(OUTPUT_DIM) * 0.1).astype(np.float32),
    }


def fixed_warmup_input() -> np.ndarray:
    """The single canonical warm-up input, shape (INPUT_DIM,), float32."""
    return np.linspace(-1.0, 1.0, INPUT_DIM, dtype=np.float32)


def expected_warmup_logits(weights: dict[str, np.ndarray]) -> np.ndarray:
    """Reference forward pass, independent from :meth:`SimpleMLP.forward`.

    Keeping the reference implementation separate means a bug shared by both
    code paths (e.g. a swapped activation) cannot mask itself during warm-up.
    """
    x = fixed_warmup_input()
    h = np.tanh(x @ weights["W1"] + weights["b1"])
    logits = h @ weights["W2"] + weights["b2"]
    return np.asarray(logits, dtype=np.float32)


class SimpleMLP:
    """Tiny MLP: tanh hidden layer, linear output (logits)."""

    def __init__(self, weights: dict[str, np.ndarray], version: str):
        self._version = version
        # Defensive copies: the model owns its buffers and must not be
        # affected if the caller later mutates the arrays it passed in.
        self._weights = {name: np.array(weights[name], copy=True) for name in LAYER_NAMES}
        self._disposed = False

    @property
    def version(self) -> str:
        return self._version

    @property
    def disposed(self) -> bool:
        return self._disposed

    def forward(self, x: np.ndarray) -> np.ndarray:
        """Run a forward pass.

        Accepts a single vector of shape ``(INPUT_DIM,)`` or a batch
        ``(N, INPUT_DIM)``; returns logits with the matching leading shape.
        """
        if self._disposed:
            raise RuntimeError(
                f"model version {self._version!r} has been disposed"
            )
        arr = np.asarray(x, dtype=np.float32)
        single = arr.ndim == 1
        if single:
            arr = arr[None, :]
        if arr.ndim != 2 or arr.shape[1] != INPUT_DIM:
            raise ValueError(f"expected input shape (..., {INPUT_DIM}), got {tuple(arr.shape)}")
        h = np.tanh(arr @ self._weights["W1"] + self._weights["b1"])
        logits = h @ self._weights["W2"] + self._weights["b2"]
        return logits[0] if single else logits

    def dispose(self) -> None:
        """Release the NumPy buffers. Idempotent."""
        if not self._disposed:
            self._weights.clear()
            self._disposed = True
