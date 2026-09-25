"""STFT, inverse STFT, framing and reconstruction diagnostics.

Conventions
-----------
* Frames are taken at frame start positions ``m * hop_length`` after padding.
* ``center=True`` (default): the signal is padded by ``n_fft // 2`` on each
  side so that frame ``m`` is centred on original sample ``m * hop``.
  Supported ``pad_mode`` values are ``"constant"`` (zero pad) and ``"reflect"``
  (edge-mirrored). Reflect padding requires ``len(x) + 1 >= n_fft``; shorter
  signals cannot be reflect-padded (NumPy limitation that we surface explicitly).
* ``center=False``: no padding; only full frames fitting inside the signal
  are produced and the ends are not reconstructed.
* Analysis and synthesis use the same window. The weighted overlap-add
  reconstruction is normalised by the window-overlap weight, so it is
  perfect whenever the intrinsic per-sample weight ``w[n]^2`` summed over
  frames (the NOLA condition for equal analysis/synthesis windows) is
  strictly positive at every reconstructed sample.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .windows import make_window

_PAD_MODES = ("constant", "reflect")


class STFTError(ValueError):
    """Invalid STFT configuration or input."""


@dataclass(frozen=True)
class STFTConfig:
    """Validated STFT parameters."""

    n_fft: int
    hop_length: int
    window_name: str
    center: bool
    pad_mode: str
    window: np.ndarray  # analysis == synthesis window, length n_fft

    @property
    def half_hop_divides(self) -> bool:
        """True when ``n_fft // 2`` is a multiple of ``hop_length``."""
        return (self.n_fft // 2) % self.hop_length == 0


def prepare_config(
    n_fft: int,
    hop_length: int,
    window_name: str = "hann",
    center: bool = True,
    pad_mode: str = "constant",
) -> STFTConfig:
    """Validate parameters and build a window."""
    if not isinstance(n_fft, (int, np.integer)) or n_fft <= 0:
        raise STFTError(f"n_fft must be a positive integer, got {n_fft!r}")
    if not isinstance(hop_length, (int, np.integer)) or hop_length <= 0:
        raise STFTError(
            f"hop_length must be a positive integer, got {hop_length!r}"
        )
    n_fft = int(n_fft)
    hop_length = int(hop_length)
    if hop_length > n_fft:
        raise STFTError(
            f"hop_length ({hop_length}) must be <= n_fft ({n_fft}); "
            "larger hops leave unreconstructable gaps"
        )
    if not isinstance(center, bool):
        raise STFTError(f"center must be bool, got {center!r}")
    if pad_mode not in _PAD_MODES:
        raise STFTError(f"pad_mode must be one of {_PAD_MODES}, got {pad_mode!r}")
    window = make_window(window_name, n_fft)
    return STFTConfig(
        n_fft=n_fft,
        hop_length=hop_length,
        window_name=window_name,
        center=center,
        pad_mode=pad_mode,
        window=window,
    )


def pad_signal(
    x: np.ndarray, n_fft: int, pad_mode: str = "constant"
) -> np.ndarray:
    """Pad a 1-D signal by ``n_fft // 2`` samples on each side.

    ``"constant"`` pads zeros; ``"reflect"`` mirrors without repeating the
    edge sample and requires ``x.size + 1 >= n_fft``.
    """
    x = np.asarray(x, dtype=np.float64)
    if x.ndim != 1:
        raise STFTError(f"signal must be 1-D, got shape {x.shape}")
    amt = n_fft // 2
    if amt == 0:
        return x.astype(np.float64, copy=True)
    if pad_mode == "reflect" and x.size + 1 < n_fft:
        raise STFTError(
            f"reflect padding needs len(signal) + 1 >= n_fft "
            f"(got len={x.size}, n_fft={n_fft}); use pad_mode='constant' "
            "for short signals"
        )
    np_mode = "constant" if pad_mode == "constant" else "reflect"
    return np.pad(x, (amt, amt), mode=np_mode).astype(np.float64, copy=False)


def number_of_frames(signal_len: int, config: STFTConfig) -> int:
    """Number of full frames produced for a signal of ``signal_len`` samples."""
    if signal_len < 0:
        raise STFTError("signal length must be non-negative")
    if config.center:
        padded_len = signal_len + 2 * (config.n_fft // 2)
    else:
        padded_len = signal_len
    if padded_len < config.n_fft:
        return 0
    return 1 + (padded_len - config.n_fft) // config.hop_length


def _frame_signal(xp: np.ndarray, config: STFTConfig) -> np.ndarray:
    """View overlapping frames of the (padded) signal as ``(n_frames, n_fft)``."""
    n_fft = config.n_fft
    hop = config.hop_length
    if xp.size < n_fft:
        return np.empty((0, n_fft), dtype=np.float64)
    n_frames = 1 + (xp.size - n_fft) // hop
    shape = (n_frames, n_fft)
    strides = (hop * xp.strides[0], xp.strides[0])
    return np.lib.stride_tricks.as_strided(
        xp, shape=shape, strides=strides, writeable=False
    )


def stft(x: np.ndarray, config: STFTConfig) -> np.ndarray:
    """Forward STFT.

    Returns the one-sided spectrum, complex128, shape
    ``(n_frames, n_fft // 2 + 1)``.
    """
    x = np.asarray(x, dtype=np.float64)
    if x.ndim != 1:
        raise STFTError(f"signal must be 1-D, got shape {x.shape}")
    if config.center:
        xp = pad_signal(x, config.n_fft, config.pad_mode)
    else:
        xp = x
    frames = _frame_signal(xp, config)
    if frames.shape[0] == 0:
        return np.empty((0, config.n_fft // 2 + 1), dtype=np.complex128)
    windowed = frames * config.window[np.newaxis, :]
    return np.fft.rfft(windowed, n=config.n_fft, axis=1)


def overlap_sums(
    length: int, config: STFTConfig, squared: bool
) -> np.ndarray:
    """Intrinsic window overlap-add weight over ``length`` sample positions.

    With ``squared=False`` this is ``sum_m w[n - m*hop]`` (COLA weight);
    with ``squared=True`` it is ``sum_m w[n - m*hop]^2`` — the actual
    normalisation weight when analysis and synthesis share one window.
    """
    w = config.window if not squared else config.window**2
    weight = np.zeros(length, dtype=np.float64)
    n_frames = 0
    if length >= config.n_fft:
        n_frames = 1 + (length - config.n_fft) // config.hop_length
    for m in range(n_frames):
        start = m * config.hop_length
        weight[start : start + config.n_fft] += w
    return weight


def _periodic_overlap_sum(w: np.ndarray, hop: int) -> np.ndarray:
    """Cyclic overlap-add of ``w`` on an ``n_fft``-periodic frame lattice.

    Sums ``w`` over all frame shifts that wrap onto each residue class of
    ``n_fft`` (finite number since shifts are multiples of ``hop``). This is
    the infinite-lattice overlap weight used to evaluate COLA / NOLA, free
    of finite-buffer boundary ramps.
    """
    n = w.size
    # shifts m*hop modulo n cycle with period n/gcd(n,hop)
    from math import gcd

    period = n // gcd(n, hop)
    acc = np.zeros(n, dtype=np.float64)
    for m in range(period):
        shift = (m * hop) % n
        acc += np.roll(w, shift)
    return acc


def cola_diagnostic(config: STFTConfig, length: int | None = None) -> dict:
    """Report COLA / NOLA conditions for a config.

    COLA and NOLA are intrinsic, infinite-lattice properties, evaluated here
    on one ``n_fft`` period with cyclic overlap-add (so no finite-buffer edge
    ramps pollute them). When ``length`` is given, the finite buffer used for
    a concrete signal is also inspected; boundary zeros there (positions no
    frame reaches at all) are reported separately as
    ``zero_weight_positions_all``.
    """
    n_fft, hop = config.n_fft, config.hop_length

    wcola = _periodic_overlap_sum(config.window, hop)
    wsq = _periodic_overlap_sum(config.window**2, hop)

    cola_holds = bool(np.allclose(wcola, wcola[0], atol=1e-10))
    cola_constant = float(wcola[0])

    eps = 1e-14 * max(1.0, float(np.max(np.abs(config.window)) ** 2))
    nola_zeros_period = [int(i) for i in np.flatnonzero(wsq <= eps)]
    nola_holds = len(nola_zeros_period) == 0

    result: dict = {
        "n_fft": n_fft,
        "hop_length": hop,
        "window": config.window_name,
        "center": config.center,
        "cola_holds": cola_holds,
        "cola_constant": cola_constant,
        "nola_holds": nola_holds,
        "nola_zero_positions_periodic": nola_zeros_period,
        "period_length_examined": n_fft,
    }

    if length is not None:
        if config.center:
            buf_len = length + 2 * (n_fft // 2)
        else:
            buf_len = length
        wsq_buf = overlap_sums(buf_len, config, squared=True)
        # Boundary positions are zero because no frame reaches them; they are
        # trimmed off for center=True. Report them for transparency.
        zeros_all = [int(i) for i in np.flatnonzero(wsq_buf <= eps)]
        result["buffer_length"] = buf_len
        result["zero_weight_positions_all"] = zeros_all
    return result


def istft(
    spec: np.ndarray,
    config: STFTConfig,
    signal_length: int | None = None,
) -> tuple[np.ndarray, dict]:
    """Weight-normalised inverse STFT.

    Parameters
    ----------
    spec:
        Complex spectrum of shape ``(n_frames, n_fft // 2 + 1)``.
    signal_length:
        Original signal length; required for trimming when ``center=True``.
        With ``center=False`` the output is simply truncated to this length
        if given.

    Returns
    -------
    (signal, info):
        Reconstructed real signal and a diagnostics dict. When any output
        sample has zero window-overlap weight the signal there is defined as
        zero and ``info["reconstruction_possible"]`` is False with the
        offending positions listed.
    """
    spec = np.asarray(spec)
    if spec.ndim != 2 or spec.shape[1] != config.n_fft // 2 + 1:
        raise STFTError(
            f"spectrum must have shape (n_frames, {config.n_fft // 2 + 1}), "
            f"got {spec.shape}"
        )
    n_frames = spec.shape[0]
    n_fft, hop = config.n_fft, config.hop_length

    if config.center and signal_length is None:
        raise STFTError("signal_length is required for istft with center=True")

    if config.center:
        out_len = signal_length + 2 * (n_fft // 2)
    else:
        # Only positions reached by at least one frame are part of the
        # output; the signal tail beyond the last full frame is unreachable
        # with center=False and is explicitly not reconstructed.
        out_len = (n_frames - 1) * hop + n_fft if n_frames > 0 else 0
        covered_len = out_len

    ysum = np.zeros(out_len, dtype=np.float64)
    wsum = np.zeros(out_len, dtype=np.float64)
    frames_time = np.fft.irfft(spec, n=n_fft, axis=1)
    for m in range(n_frames):
        start = m * hop
        stop = start + n_fft
        ysum[start:stop] += frames_time[m] * config.window
        wsum[start:stop] += config.window**2

    eps = 1e-14 * max(1.0, float(np.max(np.abs(config.window)) ** 2))
    zero_positions = [int(i) for i in np.flatnonzero(wsum <= eps)]
    good = wsum > eps
    y = np.zeros(out_len, dtype=np.float64)
    y[good] = ysum[good] / wsum[good]

    info: dict = {
        "n_frames": int(n_frames),
        "buffer_length": int(out_len),
        "zero_weight_positions": zero_positions,
        "reconstruction_possible": len(zero_positions) == 0,
        "min_weight": float(wsum.min()) if wsum.size else 0.0,
    }

    if config.center:
        cut = n_fft // 2
        y = y[cut : cut + signal_length]
        # Weight zero at padded boundaries is expected; only zero weight on
        # actual signal samples is a true failure.
        bad_signal = [
            i - cut
            for i in zero_positions
            if cut <= i < cut + signal_length
        ]
        info["zero_weight_positions"] = bad_signal
        info["reconstruction_possible"] = len(bad_signal) == 0
        info["signal_length"] = int(signal_length)
        info["padded_buffer_length"] = int(out_len)
        wsig = wsum[cut : cut + signal_length]
        info["min_weight"] = float(wsig.min()) if wsig.size else 0.0
    else:
        info["covered_length"] = int(covered_len)
        if signal_length is not None:
            info["uncovered_tail_length"] = int(
                max(0, signal_length - covered_len)
            )
    return y, info
