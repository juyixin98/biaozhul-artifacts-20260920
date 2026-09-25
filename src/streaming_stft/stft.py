"""STFT, inverse STFT and reconstruction diagnostics.

Conventions (all explicit, see :class:`STFTConfig`):

* frames start at ``n = 0, hop, 2*hop, ...`` up to the last frame that fits
  inside the padded signal;
* each frame is multiplied by the analysis window before the rFFT;
* inverse transform synthesises each frame with the *same* window
  (weighted overlap-add) and divides by the accumulated squared-window
  weight, i.e. WOLA reconstruction
  ``x[n] = sum_m w[n-mH] * frame_m[n-mH] / sum_m w[n-mH]^2``;
* padding: ``reflect`` (default, symmetric), ``constant`` (zero) or
  ``edge`` (replicate), applied ``nfft//2`` samples on each side when
  ``center=True`` (numpy ``pad`` semantics, ``reflect`` requires
  ``n_samples >= nfft//2``);
* ``np.fft.rfft`` is used, so ``X[m].shape == (nfft//2 + 1,)``.

The reconstruction condition for WOLA is that every output sample is covered
by at least one frame with a non-zero window weight (the *COLA / weight
coverage* condition).  :func:`window_diagnostic` checks it explicitly and
:func:`istft` reports the exact zero-weight positions instead of silently
producing zeros there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .windows import get_window

__all__ = [
    "STFTConfig",
    "STFTOutput",
    "ISTFTOutput",
    "stft",
    "istft",
    "window_diagnostic",
    "validate_config",
]

_PAD_MODES = ("reflect", "constant", "edge")
_ZERO_WEIGHT_TOL = 1e-12


@dataclass(frozen=True)
class STFTConfig:
    """STFT parameters.

    Attributes
    ----------
    nfft:
        Window / FFT length ``N`` (samples).
    hop:
        Hop size (synthesis stride) ``H`` samples, ``1 <= H <= N``.
    window:
        Window name (``hann``/``hamming``/``rect``/``blackman``) or an
        explicit 1-D array of length ``nfft``.
    center:
        When ``True`` the signal is padded by ``nfft // 2`` on both sides so
        that frame *m* is centered on sample ``m * hop``.  When ``False``
        only frames fully inside the signal are taken; samples never covered
        by a frame cannot be reconstructed.
    pad_mode:
        NumPy padding mode used when ``center=True``.
    periodic_window:
        Whether a named window is periodic (preferred for STFT) or symmetric.
    """

    nfft: int = 256
    hop: int = 128
    window: Any = "hann"
    center: bool = True
    pad_mode: str = "reflect"
    periodic_window: bool = True

    def resolved_window(self) -> np.ndarray:
        """Return the analysis/synthesis window as a float64 array."""
        if isinstance(self.window, np.ndarray):
            w = self.window.astype(np.float64, copy=True)
        elif isinstance(self.window, (list, tuple)):
            w = np.asarray(self.window, dtype=np.float64)
        else:
            w = get_window(str(self.window), self.nfft, periodic=self.periodic_window)
        if w.ndim != 1 or w.shape[0] != self.nfft:
            raise ValueError(
                f"window must be 1-D of length nfft={self.nfft}, "
                f"got shape {w.shape}"
            )
        return w

    def descriptor(self) -> str:
        """Stable short string identifying the configuration."""
        if isinstance(self.window, str):
            wname = self.window
        else:
            wname = "custom"
        center = "center" if self.center else "no-center"
        return f"nfft={self.nfft}_hop={self.hop}_w={wname}_{center}_{self.pad_mode}"


def validate_config(config: STFTConfig) -> None:
    """Validate parameter relationships; raises ``ValueError`` with a clear message."""
    if not isinstance(config.nfft, (int, np.integer)) or config.nfft <= 0:
        raise ValueError(f"nfft must be a positive integer, got {config.nfft!r}")
    if not isinstance(config.hop, (int, np.integer)) or config.hop <= 0:
        raise ValueError(f"hop must be a positive integer, got {config.hop!r}")
    if config.hop > config.nfft:
        raise ValueError(
            f"hop ({config.hop}) must be <= nfft ({config.nfft}): "
            "frames would not overlap and gaps could not be reconstructed"
        )
    if config.pad_mode not in _PAD_MODES:
        raise ValueError(
            f"pad_mode must be one of {_PAD_MODES}, got {config.pad_mode!r}"
        )
    # Materialise the window once so an invalid custom window fails here too.
    config.resolved_window()


@dataclass(frozen=True)
class STFTOutput:
    """Result of :func:`stft`."""

    spectrogram: np.ndarray  # complex64/128, shape (n_frames, n_bins)
    n_samples: int  # original (un-padded) signal length
    n_frames: int
    pad_left: int
    pad_right: int
    window: np.ndarray
    config: STFTConfig


@dataclass(frozen=True)
class ISTFTOutput:
    """Result of :func:`istft`."""

    signal: np.ndarray  # shape (n_samples,), float64
    weights: np.ndarray  # per-sample summed squared window, length n_samples
    zero_weight: np.ndarray  # indices with (near-)zero reconstruction weight
    covered: bool  # True when every sample has non-zero weight
    min_weight: float
    n_frames_used: int = 0
    diagnostics: str = ""


@dataclass(frozen=True)
class WindowDiagnostic:
    """Overlap-weight coverage information."""

    weights: np.ndarray  # length equals padded signal length
    zero_positions: np.ndarray
    covered: bool
    min_nonzero_weight: float
    weight_is_constant: bool
    weight_constant_value: float
    frame_count: int


def frame_plan(config: STFTConfig, n_samples: int) -> tuple[int, int, int]:
    """Return ``(base_length, n_frames, grid_length)`` for a signal of length n.

    * ``base_length`` is the length after centering padding (before the
      zero-filled tail);
    * ``n_frames`` covers both the frames fully inside that base and any
      extra frames needed to reach the last original sample;
    * ``grid_length = (n_frames - 1) * hop + nfft``.
    """
    pad_left = config.nfft // 2 if config.center else 0
    base_length = 2 * pad_left + n_samples if config.center else n_samples
    if n_samples == 0 or base_length < config.nfft:
        return base_length, 0, 0

    f_in_base = int((base_length - config.nfft) // config.hop) + 1
    core_end = pad_left + n_samples
    # Smallest frame index whose window reaches/passes the core end:
    # m*hop + nfft >= core_end  =>  m >= (core_end - nfft) / hop.
    f_covering = int(np.ceil((core_end - config.nfft) / config.hop)) + 1
    n_frames = max(f_in_base, f_covering)
    grid_length = (n_frames - 1) * config.hop + config.nfft
    return base_length, n_frames, grid_length


def build_grid(
    signal: np.ndarray, config: STFTConfig
) -> tuple[np.ndarray, int, int]:
    """Build the deterministic framing grid shared by bulk and streaming paths.

    Steps:

    1. ``center=True``: pad ``nfft//2`` samples on each side with
       ``pad_mode`` (``reflect`` / ``edge`` / ``constant``).
       ``center=False``: no padding.
    2. Zero-extend on the right until the last planned frame window reaches
       the end of the signal, so every original sample is reached by at
       least one frame window (see :func:`frame_plan`).

    Returns ``(grid, pad_left, n_frames)``.
    """
    n = signal.shape[0]
    if config.center:
        pad_left = config.nfft // 2
        if config.pad_mode == "reflect" and n <= pad_left:
            raise ValueError(
                f"reflect padding needs more than {pad_left} samples "
                f"(nfft//2), got {n}; use pad_mode='edge' or 'constant', "
                "or center=False"
            )
        base = np.pad(signal, (pad_left, pad_left), mode=config.pad_mode)
    else:
        pad_left = 0
        base = signal

    _, n_frames, grid_length = frame_plan(config, n)
    if n_frames == 0:
        return base, pad_left, 0
    if grid_length > base.shape[0]:
        grid = np.pad(base, (0, grid_length - base.shape[0]), mode="constant")
    else:
        grid = base
    return grid, pad_left, n_frames


# Backwards-compatible internal alias used by :func:`stft`.
def _pad_signal(
    signal: np.ndarray, config: STFTConfig
) -> tuple[np.ndarray, int, int, int]:
    n = signal.shape[0]
    grid, pad_left, n_frames = build_grid(signal, config)
    pad_right = grid.shape[0] - pad_left - n
    return grid, pad_left, int(pad_right), n_frames


def stft(signal: np.ndarray, config: STFTConfig | None = None) -> STFTOutput:
    """Compute the short-time Fourier transform of a real 1-D signal."""
    config = config or STFTConfig()
    validate_config(config)
    x = np.asarray(signal)
    if x.ndim != 1:
        raise ValueError(f"signal must be 1-D, got shape {x.shape}")
    x = x.astype(np.float64, copy=False)
    n = x.shape[0]
    window = config.resolved_window()

    if n == 0:
        return STFTOutput(
            spectrogram=np.zeros((0, config.nfft // 2 + 1), dtype=np.complex128),
            n_samples=0,
            n_frames=0,
            pad_left=config.nfft // 2 if config.center else 0,
            pad_right=config.nfft // 2 if config.center else 0,
            window=window,
            config=config,
        )

    padded, pad_left, pad_right, n_frames = _pad_signal(x, config)

    # Gather frames with stride tricks (read-only view, no data copy).
    strides = (config.hop * padded.strides[0], padded.strides[0])
    shape = (n_frames, config.nfft)
    frames = np.lib.stride_tricks.as_strided(
        padded, shape=shape, strides=strides, writeable=False
    )
    spectrum = np.fft.rfft(frames * window, n=config.nfft, axis=1)

    return STFTOutput(
        spectrogram=spectrum,
        n_samples=n,
        n_frames=n_frames,
        pad_left=pad_left,
        pad_right=pad_right,
        window=window,
        config=config,
    )


def window_diagnostic(
    config: STFTConfig,
    padded_length: int,
    n_frames: int,
    original_length: int,
    pad_left: int,
) -> WindowDiagnostic:
    """Accumulate ``sum_m w^2`` over frame positions and report coverage.

    The diagnostic is evaluated on the **original** (un-padded) sample range
    ``[pad_left, pad_left + original_length)`` of the padded grid, which is
    exactly the range :func:`istft` returns.
    """
    window = config.resolved_window()
    w2 = window * window
    weights = np.zeros(padded_length, dtype=np.float64)
    for m in range(n_frames):
        start = m * config.hop
        weights[start : start + config.nfft] += w2

    lo, hi = pad_left, pad_left + original_length
    core = weights[lo:hi] if original_length > 0 else weights[:0]
    zero_positions = np.flatnonzero(core <= _ZERO_WEIGHT_TOL)

    # COLA constancy is an *interior* property: exclude a full window width
    # at each end, where fewer than the steady-state number of windows
    # contribute (the right edge may have no trailing frame at all).
    ramp = config.nfft
    if core.size > 2 * ramp:
        interior = core[ramp : core.size - ramp]
    else:
        interior = core[:0]
    interior_nonzero = interior[interior > _ZERO_WEIGHT_TOL]
    if interior_nonzero.size:
        i_spread = float(
            interior_nonzero.max() - interior_nonzero.min()
        )
        constant = i_spread <= 1e-9 * max(1.0, float(interior_nonzero.max()))
        const_val = float(interior_nonzero.mean()) if constant else float("nan")
    else:
        constant = False
        const_val = float("nan")

    nonzero = core[core > _ZERO_WEIGHT_TOL]
    mn = float(nonzero.min()) if nonzero.size else 0.0
    return WindowDiagnostic(
        weights=core,
        zero_positions=zero_positions,
        covered=zero_positions.size == 0 and core.size > 0,
        min_nonzero_weight=mn,
        weight_is_constant=constant,
        weight_constant_value=const_val,
        frame_count=n_frames,
    )


def overlap_add(
    time_frames: np.ndarray,
    window: np.ndarray,
    hop: int,
    grid_length: int,
    frame_offset: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Accumulate windowed frames and squared-window weights on a grid.

    ``time_frames`` has shape ``(n_frames, nfft)`` (inverse-FFT result).
    Frame ``m`` is placed at grid position ``(frame_offset + m) * hop``;
    frames whose window would run past ``grid_length`` are skipped (the
    streaming tail keeps them for a later flush).
    Returns ``(accumulator, weight_sum)``, each of length ``grid_length``.
    """
    nfft = window.shape[0]
    w2 = window * window
    acc = np.zeros(grid_length, dtype=np.float64)
    wsum = np.zeros(grid_length, dtype=np.float64)
    for m in range(time_frames.shape[0]):
        start = (frame_offset + m) * hop
        end = start + nfft
        if end > grid_length:
            continue
        acc[start:end] += time_frames[m] * window
        wsum[start:end] += w2
    return acc, wsum


def normalize_window_sum(
    acc: np.ndarray, wsum: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Divide by the overlap weight; zero-weight samples become exactly 0.

    Returns ``(signal, zero_weight_indices)``.
    """
    signal = np.zeros_like(acc)
    good = wsum > _ZERO_WEIGHT_TOL
    signal[good] = acc[good] / wsum[good]
    zero = np.flatnonzero(~good)
    return signal, zero


def istft(
    output: STFTOutput,
    *,
    signal_length: int | None = None,
) -> ISTFTOutput:
    """Inverse STFT via windowed overlap-add with weight normalisation.

    Parameters
    ----------
    output:
        The object returned by :func:`stft`.
    signal_length:
        Desired output length; defaults to the original signal length stored
        in ``output``.  May be shorter (streaming flush with fewer input
        samples) but not longer than the padded grid covers.
    """
    config = output.config
    validate_config(config)
    n = output.n_samples if signal_length is None else int(signal_length)
    if n > output.n_samples:
        raise ValueError(
            f"requested signal length {n} exceeds the {output.n_samples} "
            "samples covered by this STFT"
        )
    window = output.window
    n_bins = config.nfft // 2 + 1
    if output.spectrogram.shape[1:] != (n_bins,):
        raise ValueError(
            f"spectrogram bin count {output.spectrogram.shape[1:]} does not "
            f"match nfft={config.nfft}"
        )

    # The grid was fixed at analysis time; signal_length may only trim it.
    grid_length = output.pad_left + output.pad_right + output.n_samples

    n_frames = output.spectrogram.shape[0]
    time_frames = np.fft.irfft(output.spectrogram, n=config.nfft, axis=1)
    acc, wsum = overlap_add(time_frames, window, config.hop, grid_length)

    lo = output.pad_left
    hi = lo + n
    if hi > grid_length:
        raise ValueError(
            f"requested signal length {n} exceeds reconstructed grid "
            f"{grid_length - output.pad_left} (pad_left={output.pad_left})"
        )
    recovered, zero = normalize_window_sum(acc[lo:hi], wsum[lo:hi])

    diag = window_diagnostic(config, grid_length, n_frames, n, output.pad_left)
    message = _build_diagnostic_message(config, diag)
    min_w = float(diag.min_nonzero_weight) if diag.covered else 0.0

    return ISTFTOutput(
        signal=recovered,
        weights=wsum[lo:hi].copy(),
        zero_weight=zero,
        covered=diag.covered,
        min_weight=min_w,
        n_frames_used=int(n_frames),
        diagnostics=message,
    )


def _build_diagnostic_message(config: STFTConfig, diag: WindowDiagnostic) -> str:
    parts = [
        f"nfft={config.nfft}, hop={config.hop}, frames={diag.frame_count}",
    ]
    if diag.weight_is_constant:
        parts.append(
            f"interior weight sum is CONSTANT = {diag.weight_constant_value:.6g} "
            "(perfect COLA: normalisation is uniform)"
        )
    else:
        parts.append(
            "interior weight sum is NOT constant (sample-wise normalisation)"
        )
    if diag.covered:
        parts.append(
            f"all samples covered; min squared-window weight = "
            f"{diag.min_nonzero_weight:.6g}"
        )
    else:
        n_zero = diag.zero_positions.size
        preview = ", ".join(str(int(i)) for i in diag.zero_positions[:8])
        more = " ..." if n_zero > 8 else ""
        parts.append(
            f"NOT RECONSTRUCTIBLE: {n_zero} sample(s) have zero window weight "
            f"(first indices: {preview}{more}); increase overlap (smaller hop) "
            "or use a window that is non-zero across its full support "
            "(e.g. hamming/rect)"
        )
    return "; ".join(parts)
