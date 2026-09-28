"""Planning/dispatch engine with a size- and kernel-bound plan cache.

A cached plan binds the *full* problem size (n, batch, embedding m, dtype),
a digest of the coefficient content (so two different Toeplitz matrices of
the same shape never share a plan), and a digest of the kernel code/library
version.  Repeated calls with the same Toeplitz definition reuse the
embedding spectrum (one forward FFT), while per-vector transforms still run.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .config import SETTINGS, Settings
from .errors import ErrorCode, KernelError
from .inputs import Problem
from .kernels import (
    KERNEL_NAME,
    KERNEL_VERSION,
    build_embedding,
    coefficient_fingerprint,
    direct_multiply,
    embedding_spectrum,
    fft_multiply_embedding,
    kernel_digest,
    memory_scale_report,
    min_embedding_size,
    padded_embedding_size,
)
from .logging_ctx import get_logger


@dataclass(frozen=True)
class Plan:
    n: int
    batch: int
    m: int
    mode: str
    precision: str
    dtype: str
    path: str  # "tiny_explicit" | "embedding_fft"
    coefficient_digest: str
    kernel_digest: str
    kernel_name: str
    kernel_version: str


@dataclass(frozen=True)
class _CacheEntry:
    plan: Plan
    spectrum: np.ndarray
    embedding: np.ndarray


@dataclass
class EngineResult:
    output: np.ndarray
    plan: Plan
    cache_hit: bool
    stats: dict[str, Any] = field(default_factory=dict)


class Engine:
    """Thread-safe LRU cache of embedding spectra plus path selection."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or SETTINGS
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------ #
    # Path selection
    # ------------------------------------------------------------------ #
    def _select_path(self, problem: Problem) -> str:
        if problem.kernel == "tiny_explicit":
            return "tiny_explicit"
        if problem.kernel == "embedding_fft":
            return "embedding_fft"
        # auto: the explainable explicit path for very small problems,
        # FFT embedding otherwise.
        if problem.n <= self._settings.tiny_n_explicit:
            return "tiny_explicit"
        return "embedding_fft"

    def kernel_summary(self) -> dict[str, str]:
        return {
            "kernel_name": KERNEL_NAME,
            "kernel_version": KERNEL_VERSION,
            "kernel_digest": kernel_digest(),
        }

    # ------------------------------------------------------------------ #
    # Cache
    # ------------------------------------------------------------------ #
    def _cache_key(self, problem: Problem, m: int, path: str) -> str:
        coeff = coefficient_fingerprint(problem.first_column, problem.first_row, m)
        return "|".join([
            path,
            problem.mode,
            problem.precision,
            str(problem.n),
            str(problem.batch),
            str(m),
            coeff,
            kernel_digest(),
        ])

    def _get_cached(self, key: str) -> _CacheEntry | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self.misses += 1
                return None
            self._cache.move_to_end(key)
            self.hits += 1
            return entry

    def _put_cached(self, key: str, entry: _CacheEntry) -> None:
        with self._lock:
            self._cache[key] = entry
            self._cache.move_to_end(key)
            while len(self._cache) > self._settings.cache_size:
                self._cache.popitem(last=False)

    def cache_info(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._cache),
                "capacity": self._settings.cache_size,
                "hits": self.hits,
                "misses": self.misses,
            }

    def reset_cache_stats(self) -> None:
        with self._lock:
            self.hits = 0
            self.misses = 0

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def run(self, problem: Problem) -> EngineResult:
        log = get_logger()
        path = self._select_path(problem)
        n, batch = problem.n, problem.batch
        log.info("plan step=select_path n=%d batch=%d path=%s mode=%s precision=%s",
                 n, batch, path, problem.mode, problem.precision)

        if path == "tiny_explicit":
            plan = Plan(
                n=n, batch=batch, m=n, mode=problem.mode,
                precision=problem.precision, dtype=str(problem.value_dtype),
                path=path, coefficient_digest=coefficient_fingerprint(
                    problem.first_column, problem.first_row, n),
                **self.kernel_summary(),
            )
            output = direct_multiply(
                problem.first_column, problem.first_row, problem.vectors
            )
            log.info("compute step=tiny_explicit_done elements=%d", batch * n)
            return EngineResult(output=output, plan=plan, cache_hit=False,
                                stats={"forward_transforms_per_vector": 0})

        m = padded_embedding_size(n)
        if m < min_embedding_size(n):
            # Defensive: next_fast_len must never shrink below 2n-1.
            raise KernelError(
                ErrorCode.ALIASING_UNSAFE,
                f"planned embedding m={m} < 2n-1={min_embedding_size(n)}",
            )
        key = self._cache_key(problem, m, path)
        entry = self._get_cached(key)
        cache_hit = entry is not None
        if entry is None:
            log.info("compute step=build_embedding m=%d (minimum=%d) cache=miss",
                     m, min_embedding_size(n))
            embedding = build_embedding(problem.first_column, problem.first_row, m)
            spectrum = embedding_spectrum(embedding)
            plan = Plan(
                n=n, batch=batch, m=m, mode=problem.mode,
                precision=problem.precision, dtype=str(problem.value_dtype),
                path=path,
                coefficient_digest=coefficient_fingerprint(
                    problem.first_column, problem.first_row, m),
                **self.kernel_summary(),
            )
            entry = _CacheEntry(plan=plan, spectrum=spectrum, embedding=embedding)
            self._put_cached(key, entry)
        else:
            log.info("compute step=reuse_embedding_spectrum m=%d cache=hit", m)

        try:
            output, stats = fft_multiply_embedding(
                problem.first_column, problem.first_row, problem.vectors,
                v_hat=entry.spectrum, m=m,
            )
        except (ValueError, np.linalg.LinAlgError) as exc:
            raise KernelError(
                ErrorCode.KERNEL_FAILED, f"FFT kernel failed: {exc}"
            ) from exc

        stats["cache_hit"] = cache_hit
        stats["memory"] = memory_scale_report(
            n, batch, m, problem.value_dtype)
        log.info(
            "compute step=fft_done m=%d batch=%d cache_hit=%s last_errors_checked_later",
            m, batch, cache_hit,
        )
        return EngineResult(output=output, plan=entry.plan,
                            cache_hit=cache_hit, stats=stats)
