"""Compute kernels: circulant embedding and FFT multiplication.

Circulant embedding (no-aliasing construction)
----------------------------------------------
For an n x n Toeplitz matrix ``T`` with first column ``c`` and first row
``r`` (``c[0] == r[0]``), define the length-``m`` vector

    v = [ c[0], c[1], ..., c[n-1], r[n-1], r[n-2], ..., r[1] | 0 ... 0 ]
        |<--------- n entries -------->|<------ n-1 entries ----->| pad

with ``m >= 2*n - 1``.  The circulant matrix ``C`` having ``v`` as its first
column contains ``T`` in its leading n x n block; padding ``x`` to length
``m`` and taking the first ``n`` entries of ``ifft(fft(v) * fft(x))`` gives
``T x`` exactly in exact arithmetic.  The bound ``m >= 2n-1`` is precisely
the circular-convolution anti-aliasing condition (linear convolution of two
length-n sequences needs 2n-1 points).

Real input uses the real transform (``rfft``/``irfft``) and returns real
output; complex input uses the full complex transform.  Output precision is
the precision requested at the boundary (float32/64 or complex64/128).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
from scipy import fft as sp_fft

KERNEL_NAME = "circulant_embedding_scipy_fft"
KERNEL_VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #

def min_embedding_size(n: int) -> int:
    """Smallest alias-free embedding length: 2n - 1."""
    if n < 1:
        raise ValueError("n must be >= 1")
    return 2 * n - 1


def padded_embedding_size(n: int) -> int:
    """Smallest fast FFT length >= 2n-1 (never smaller, so no aliasing)."""
    target = min_embedding_size(n)
    size = int(sp_fft.next_fast_len(target))
    # Defensive guarantee independent of backend quirks.
    return max(size, target)


# --------------------------------------------------------------------------- #
# Embedding and direct kernels
# --------------------------------------------------------------------------- #

def build_embedding(c: np.ndarray, r: np.ndarray, m: int) -> np.ndarray:
    """Assemble the circulant first column of length ``m``.

    Layout (length m, ``m >= 2n-1``)::

        v = [ c[0..n-1],  0 ... 0,  r[n-1], r[n-2], ..., r[1] ]
            |<- n entries ->|<pad>  |<------- n-1 entries ------->|
                                            (placed at the END)

    The reversed ``r`` tail must live at indices ``[m-n+1, m)``, not at
    ``[n, 2n-1)``: a circulant wraps a negative lag ``i-j`` to ``m-(j-i)``,
    and only the end placement makes ``v[m-d] = r[d]`` for every
    ``d = 1..n-1``.  When ``m == 2n-1`` the two placements coincide.  The
    shared element ``c[0] == r[0]`` is used exactly once.
    """
    n = c.shape[0]
    if r.shape[0] != n:
        raise ValueError("c and r must share length n")
    if m < 2 * n - 1:
        raise ValueError(f"embedding m={m} would alias (need >= {2*n-1})")
    v = np.zeros(m, dtype=c.dtype)
    v[:n] = c
    v[m - n + 1 :] = r[1:][::-1]
    return v


def toeplitz_dense(c: np.ndarray, r: np.ndarray) -> np.ndarray:
    """Explicitly construct the Toeplitz matrix straight from the definition.

    ``T[i, j] = c[i - j]`` when ``i >= j`` (on/below diagonal), otherwise
    ``r[j - i]`` (above diagonal).  O(n^2); used by the explainable tiny
    path and as a building block for cross-checks.
    """
    n = c.shape[0]
    idx = np.arange(n)
    d = idx[:, None] - idx[None, :]
    t = np.where(d >= 0, c[np.maximum(d, 0)], r[-d])
    return np.ascontiguousarray(t.astype(c.dtype, copy=False))


def direct_multiply(c: np.ndarray, r: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Reference / explainable path: build T and compute ``x @ T.T``.

    ``x`` has shape ``(batch, n)``.  No conjugation is taken: a general
    Toeplitz module is not Hermitian.
    """
    t = toeplitz_dense(c, r)
    return np.ascontiguousarray(x @ t.T)


# --------------------------------------------------------------------------- #
# FFT kernel
# --------------------------------------------------------------------------- #

def embedding_spectrum(v: np.ndarray) -> np.ndarray:
    """Transform of the circulant first column.

    Real input -> one-sided real FFT; complex input -> full FFT.
    """
    if np.iscomplexobj(v):
        return sp_fft.fft(v, overwrite_x=False, workers=-1)
    return sp_fft.rfft(v, overwrite_x=False, workers=-1)


def fft_multiply_embedding(c: np.ndarray, r: np.ndarray, x: np.ndarray,
                           v_hat: np.ndarray | None = None,
                           m: int | None = None) -> tuple[np.ndarray, dict]:
    """Compute ``T @ x_k`` for every row ``x_k`` of ``x`` via FFT embedding.

    Returns ``(y, stats)`` where ``y`` has ``x``'s shape ``(batch, n)`` and
    ``stats`` reports the embedding size and transform counts.  ``v_hat`` may
    be a precomputed/cached transform of the embedding; when omitted it is
    built here.  Padding to exactly ``m`` on the inverse transform keeps the
    no-aliasing contract explicit even when ``m`` is a non-power-of-two.
    """
    n = c.shape[0]
    m = int(m) if m is not None else padded_embedding_size(n)
    if m < min_embedding_size(n):
        raise ValueError(f"embedding m={m} aliases (need >= {2*n-1})")
    complex_mode = np.iscomplexobj(c)

    if v_hat is None:
        v = build_embedding(c, r, m)
        v_hat = embedding_spectrum(v)

    batch = x.shape[0]
    if complex_mode:
        x_padded = np.zeros((batch, m), dtype=c.dtype)
        x_padded[:, :n] = x
        x_hat = sp_fft.fft(x_padded, axis=1, workers=-1)
        y_full = sp_fft.ifft(v_hat[None, :] * x_hat, n=m, axis=1, workers=-1)
        y = y_full[:, :n]
    else:
        real_dtype = c.dtype
        x_padded = np.zeros((batch, m), dtype=real_dtype)
        x_padded[:, :n] = x
        x_hat = sp_fft.rfft(x_padded, axis=1, workers=-1)
        y_full = sp_fft.irfft(v_hat[None, :] * x_hat, n=m, axis=1, workers=-1)
        y = y_full[:, :n].astype(real_dtype, copy=False)

    stats = {
        "n": n,
        "m": m,
        "m_minimum": min_embedding_size(n),
        "m_is_power_of_two": m > 0 and (m & (m - 1)) == 0,
        "batch": batch,
        "mode": "complex" if complex_mode else "real",
        "forward_transforms_per_vector": 1,
        "embedding_transform_reused": True,
    }
    return np.ascontiguousarray(y.astype(c.dtype, copy=False)), stats


# --------------------------------------------------------------------------- #
# Kernel identity / fingerprint (cache keys bind full sizes and kernel digest)
# --------------------------------------------------------------------------- #

def kernel_digest() -> str:
    """Stable digest describing the kernel implementation."""
    h = hashlib.sha256()
    h.update(KERNEL_NAME.encode())
    h.update(KERNEL_VERSION.encode())
    h.update(np.__version__.encode())
    h.update(scipy_version().encode())
    return h.hexdigest()[:16]


def scipy_version() -> str:
    from scipy import __version__ as _scipy_v
    return _scipy_v


def coefficient_fingerprint(c: np.ndarray, r: np.ndarray, m: int) -> str:
    """Content digest of the full Toeplitz definition plus embedding size."""
    h = hashlib.sha256()
    h.update(np.ascontiguousarray(c).tobytes())
    h.update(np.ascontiguousarray(r).tobytes())
    h.update(str(m).encode())
    h.update(str(c.dtype).encode())
    return h.hexdigest()[:16]


def memory_scale_report(n: int, batch: int, m: int,
                        dtype: np.dtype) -> dict:
    """Report working-set sizes in elements/bytes (no allocation happens)."""
    complex_mode = np.iscomplexobj(np.dtype(dtype).type(0))
    elem = np.dtype(dtype).itemsize
    spectrum_elems = m if complex_mode else (m // 2 + 1)
    spectrum_bytes = spectrum_elems * (2 * elem if not complex_mode else elem)
    # Real FFT spectrum packs two float-size components per frequency.
    plan_bytes = spectrum_bytes
    vectors_bytes = batch * m * elem
    dense_bytes = n * n * elem
    return {
        "n": n,
        "batch": batch,
        "embedding_m": m,
        "dtype": str(dtype),
        "embedding_elements": m,
        "embedding_bytes": m * elem,
        "spectrum_elements": spectrum_elems,
        "spectrum_bytes": spectrum_bytes,
        "padded_batch_bytes": vectors_bytes,
        "estimated_working_bytes": plan_bytes + 2 * vectors_bytes,
        "dense_reference_bytes": dense_bytes,
        "memory_saving_vs_dense_ratio": round(
            dense_bytes / max(plan_bytes + vectors_bytes, 1), 3
        ),
    }
