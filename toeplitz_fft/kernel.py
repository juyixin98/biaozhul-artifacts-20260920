"""FFT compute kernel: Toeplitz matvec and batched matmat via circulant embedding.

Modes
-----
- ``"auto"``: complex path iff any of c, r, x has a complex dtype.
- ``"real"``: forces the real path (scipy.fft.rfft/irfft). Complex inputs are
  accepted only if their imaginary part is exactly zero, otherwise
  ``UnsupportedModeError`` is raised.
- ``"complex"``: forces the complex path (scipy.fft.fft/ifft), output complex.

Output precision is explicit and comes from ``ToeplitzConfig``: the real path
returns ``config.real_dtype``, the complex path ``config.complex_dtype``.

Tiny problems (including 1x1) go through the exact same embedding + FFT path;
there is no dense shortcut anywhere in the kernel.
"""

from __future__ import annotations

import numpy as np
import scipy.fft

from .cache import PlanCache, kernel_digest
from .config import ToeplitzConfig
from .embedding import (
    circulant_first_column,
    embedding_length,
    validate_first_column_row,
)
from .errors import ShapeMismatchError, UnsupportedModeError

_VALID_MODES = ("auto", "real", "complex")


def resolve_mode(mode: str, *arrays: np.ndarray) -> str:
    """Resolve "auto" and validate real-mode compatibility with the data."""
    if mode not in _VALID_MODES:
        raise UnsupportedModeError(f"mode must be one of {_VALID_MODES}, got {mode!r}")
    has_complex = any(np.iscomplexobj(a) for a in arrays)
    if mode == "auto":
        return "complex" if has_complex else "real"
    if mode == "real" and has_complex:
        for a in arrays:
            if np.iscomplexobj(a) and not np.all(np.imag(a) == 0):
                raise UnsupportedModeError(
                    "mode='real' but input has a nonzero imaginary part"
                )
        return "real"
    return mode


class ToeplitzOperator:
    """A prepared Toeplitz operator holding the FFT of its circulant column."""

    def __init__(
        self,
        c: np.ndarray,
        r: np.ndarray,
        *,
        mode: str,
        config: ToeplitzConfig,
    ) -> None:
        self.config = config
        self.mode = mode
        self._real_dtype = np.dtype(config.real_dtype)
        self._complex_dtype = np.dtype(config.complex_dtype)

        if mode == "real":
            # resolve_mode already guaranteed zero imaginary parts; .real is a
            # no-op for real input and a safe projection otherwise.
            self._c = np.ascontiguousarray(np.asarray(c).real.astype(self._real_dtype))
            self._r = np.ascontiguousarray(np.asarray(r).real.astype(self._real_dtype))
        else:
            self._c = np.ascontiguousarray(np.asarray(c, dtype=self._complex_dtype))
            self._r = np.ascontiguousarray(np.asarray(r, dtype=self._complex_dtype))

        validate_first_column_row(
            self._c, self._r, config.consistency_rtol, config.consistency_atol
        )

        self.m = int(self._c.shape[0])
        self.n = int(self._r.shape[0])
        self.L = embedding_length(self.m, self.n, config.pad_to_power_of_two)

        col = circulant_first_column(self._c, self._r, self.L)
        if mode == "real":
            self._spectrum = scipy.fft.rfft(col).astype(self._complex_dtype, copy=False)
        else:
            self._spectrum = scipy.fft.fft(col).astype(self._complex_dtype, copy=False)

    @property
    def digest(self) -> str:
        return kernel_digest(
            self._c,
            self._r,
            mode=self.mode,
            L=self.L,
            real_dtype=self.config.real_dtype,
            complex_dtype=self.config.complex_dtype,
        )

    @property
    def output_dtype(self) -> np.dtype:
        return self._real_dtype if self.mode == "real" else self._complex_dtype

    def apply(self, X: np.ndarray) -> np.ndarray:
        """Apply the operator to a vector (n,) or a batch of columns (n, k)."""
        X = np.asarray(X)
        squeeze = X.ndim == 1
        if squeeze:
            X = X[:, np.newaxis]
        if X.ndim != 2 or X.shape[0] != self.n:
            raise ShapeMismatchError(
                f"input must have shape ({self.n},) or ({self.n}, k), got {X.shape}"
            )
        if self.mode == "real":
            Xw = np.asarray(X).real.astype(self._real_dtype, copy=False)
        else:
            Xw = np.asarray(X, dtype=self._complex_dtype)
        padded = np.zeros((self.L, Xw.shape[1]), dtype=Xw.dtype)
        padded[: self.n] = Xw

        if self.mode == "real":
            spec_x = scipy.fft.rfft(padded, axis=0).astype(self._complex_dtype, copy=False)
            Y = scipy.fft.irfft(spec_x * self._spectrum[:, np.newaxis],
                                n=self.L, axis=0)
        else:
            spec_x = scipy.fft.fft(padded, axis=0)
            Y = scipy.fft.ifft(spec_x * self._spectrum[:, np.newaxis], axis=0)

        out = np.ascontiguousarray(Y[: self.m].astype(self.output_dtype, copy=False))
        return out[:, 0] if squeeze else out


def get_operator(
    c: np.ndarray,
    r: np.ndarray,
    *,
    mode: str,
    config: ToeplitzConfig,
    cache: PlanCache | None,
) -> tuple[ToeplitzOperator, bool]:
    """Return a (possibly cached) prepared operator and whether it was a hit.

    The cache key binds the resolved mode, both dtypes, the embedding length
    and the exact bytes of the canonicalized c and r.
    """
    probe_mode = resolve_mode(mode, np.asarray(c), np.asarray(r))
    if probe_mode == "real":
        cc = np.ascontiguousarray(np.asarray(c).real.astype(config.real_dtype))
        rr = np.ascontiguousarray(np.asarray(r).real.astype(config.real_dtype))
    else:
        cc = np.ascontiguousarray(np.asarray(c, dtype=config.complex_dtype))
        rr = np.ascontiguousarray(np.asarray(r, dtype=config.complex_dtype))
    L = embedding_length(cc.shape[0], rr.shape[0], config.pad_to_power_of_two)
    key = kernel_digest(
        cc, rr, mode=probe_mode, L=L,
        real_dtype=config.real_dtype, complex_dtype=config.complex_dtype,
    )
    if cache is not None:
        op, hit = cache.get(key)
        if hit:
            return op, True
    op = ToeplitzOperator(cc, rr, mode=probe_mode, config=config)
    if cache is not None:
        cache.put(key, op)
    return op, False


def matvec(
    c: np.ndarray,
    r: np.ndarray,
    x: np.ndarray,
    *,
    mode: str = "auto",
    config: ToeplitzConfig | None = None,
    cache: PlanCache | None = None,
) -> tuple[np.ndarray, dict]:
    """Compute T @ x. Returns (result, meta) with mode/L/digest/cache_hit."""
    config = config or ToeplitzConfig()
    op, hit = get_operator(c, r, mode=mode, config=config, cache=cache)
    resolved = resolve_mode(mode, np.asarray(c), np.asarray(r), np.asarray(x))
    if resolved != op.mode:
        # x introduced complex data while c, r are real: rebuild in complex mode.
        op, hit = get_operator(c, r, mode=resolved, config=config, cache=cache)
    y = op.apply(np.asarray(x))
    return y, {
        "m": op.m, "n": op.n, "L": op.L, "mode": op.mode,
        "dtype": str(op.output_dtype), "kernel_digest": op.digest,
        "cache_hit": hit,
    }


def matmat(
    c: np.ndarray,
    r: np.ndarray,
    X: np.ndarray,
    *,
    mode: str = "auto",
    config: ToeplitzConfig | None = None,
    cache: PlanCache | None = None,
) -> tuple[np.ndarray, dict]:
    """Compute T @ X for X of shape (n, k) (a batch of k column vectors)."""
    config = config or ToeplitzConfig()
    X = np.asarray(X)
    if X.ndim != 2:
        raise ShapeMismatchError(f"matmat expects a 2-D array (n, k), got ndim={X.ndim}")
    op, hit = get_operator(c, r, mode=mode, config=config, cache=cache)
    resolved = resolve_mode(mode, np.asarray(c), np.asarray(r), X)
    if resolved != op.mode:
        op, hit = get_operator(c, r, mode=resolved, config=config, cache=cache)
    Y = op.apply(X)
    return Y, {
        "m": op.m, "n": op.n, "L": op.L, "k": int(X.shape[1]), "mode": op.mode,
        "dtype": str(op.output_dtype), "kernel_digest": op.digest,
        "cache_hit": hit,
    }
