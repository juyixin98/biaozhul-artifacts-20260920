"""Chunked (streaming) STFT and inverse STFT.

The streaming transforms produce, frame by frame and chunk by chunk, exactly
the same numerical result as :func:`stft_service.stft.stft` /
:func:`stft_service.stft.istft` on the whole signal.

Forward stream (``center=True``, ``pad_mode="constant"`` only): the streamer
internally prepends ``n_fft // 2`` zeros and appends ``n_fft // 2`` zeros on
``finish``, matching zero-padded framing. This needs no future input (the
right-edge zeros are supplied at flush). Reflect padding is not supported
incrementally because it needs the whole signal; use the whole-signal API.

Inverse stream: frames are overlap-added as they arrive; samples stay
"pending" until a later frame can no longer modify them. ``finish`` performs
the same division by window-overlap weight and trimming as the whole-signal
iSTFT, so zero-weight positions get the same diagnostic treatment.
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import as_strided

from .stft import STFTConfig, STFTError


def _weight_epsilon(window: np.ndarray) -> float:
    return 1e-14 * max(1.0, float(np.max(np.abs(window)) ** 2))


class StreamingSTFT:
    """Incremental forward STFT.

    Push arbitrarily-sized 1-D chunks via :meth:`push`; each call returns the
    frames completed by that chunk (possibly none). :meth:`finish` appends the
    right-side zero pad and returns the remaining frames.
    """

    def __init__(self, config: STFTConfig):
        self.config = config
        n_fft = config.n_fft
        self._n_fft = n_fft
        self._hop = config.hop_length
        self._frames_emitted = 0
        if config.center:
            if config.pad_mode != "constant":
                raise STFTError(
                    "streaming STFT supports pad_mode='constant' only "
                    "(reflect padding needs the whole signal); "
                    "use the whole-signal stft() for reflect mode"
                )
            self._buf = np.zeros(n_fft // 2, dtype=np.float64)
        else:
            self._buf = np.empty(0, dtype=np.float64)
        self._finished = False

    @property
    def frames_emitted(self) -> int:
        return self._frames_emitted

    def _extract(self) -> np.ndarray:
        """Take every frame fully contained in the buffer, then slide it."""
        n_fft, hop = self._n_fft, self._hop
        size = self._buf.size
        if size < n_fft:
            return np.empty((0, n_fft // 2 + 1), dtype=np.complex128)
        n_frames = 1 + (size - n_fft) // hop
        frames = as_strided(
            self._buf,
            shape=(n_frames, n_fft),
            strides=(hop * self._buf.strides[0], self._buf.strides[0]),
            writeable=False,
        )
        windowed = np.ascontiguousarray(frames) * self.config.window
        spec = np.fft.rfft(windowed, n=n_fft, axis=1)
        # frames emitted start at 0, hop, ..., (n_frames-1)*hop of this buffer
        self._buf = self._buf[n_frames * hop :]
        self._frames_emitted += n_frames
        return spec

    def push(self, chunk: np.ndarray) -> np.ndarray:
        """Add a chunk; return newly completed frames ``(k, n_freq)``."""
        if self._finished:
            raise STFTError("push() called after finish()")
        chunk = np.asarray(chunk, dtype=np.float64)
        if chunk.ndim != 1:
            raise STFTError(f"chunks must be 1-D, got shape {chunk.shape}")
        if chunk.size:
            self._buf = np.concatenate([self._buf, chunk])
        return self._extract()

    def finish(self) -> np.ndarray:
        """Append the right zero pad and flush all remaining full frames."""
        if self._finished:
            raise STFTError("finish() called twice")
        self._finished = True
        if self.config.center:
            self._buf = np.concatenate(
                [self._buf, np.zeros(self._n_fft // 2, dtype=np.float64)]
            )
        # Samples left after the last full frame start are reached by no
        # frame, exactly as in the whole-signal transform; drop them.
        return self._extract()


class StreamingISTFT:
    """Incremental inverse STFT with weighted overlap-add normalisation.

    Feed frames (possibly several per push) in order. Frames are placed at
    their absolute positions ``m * hop``; once frame ``m`` is in, positions
    below ``(m+1)*hop`` can no longer change and :meth:`take_ready` pops them
    (in *padded* coordinates). At the end call :meth:`finish` with the
    original signal length for the same trimming as whole-signal iSTFT.
    """

    def __init__(self, config: STFTConfig):
        self.config = config
        self._n_fft = config.n_fft
        self._hop = config.hop_length
        self._frames_seen = 0
        # Buffers hold the still-live suffix starting at absolute index
        # self._base (never 0 once samples have been drained).
        self._base = 0
        self._ysum = np.empty(0, dtype=np.float64)
        self._wsum = np.empty(0, dtype=np.float64)
        self._ready = 0  # live-buffer samples no future frame can touch
        # Normalised prefix already popped by take_ready(), absolute indices
        # [0, len(self._drained)). finish() prepends it to the final result.
        self._drained = []
        self._drained_len = 0

    @property
    def frames_seen(self) -> int:
        return self._frames_seen

    @property
    def ready_count(self) -> int:
        return self._ready

    def _ensure(self, stop_abs: int) -> None:
        need = stop_abs - self._base
        if need > self._ysum.size:
            pad = need - self._ysum.size
            self._ysum = np.concatenate(
                [self._ysum, np.zeros(pad, dtype=np.float64)]
            )
            self._wsum = np.concatenate(
                [self._wsum, np.zeros(pad, dtype=np.float64)]
            )

    def push(self, spec_frames: np.ndarray) -> int:
        """Overlap-add frames; return count of ready live-buffer samples."""
        spec_frames = np.asarray(spec_frames)
        if (
            spec_frames.ndim != 2
            or spec_frames.shape[1] != self._n_fft // 2 + 1
        ):
            raise STFTError(
                f"frames must have shape (k, {self._n_fft // 2 + 1}), "
                f"got {spec_frames.shape}"
            )
        time_frames = np.fft.irfft(spec_frames, n=self._n_fft, axis=1)
        for row in time_frames:
            m = self._frames_seen
            start = m * self._hop - self._base
            stop = start + self._n_fft
            self._ensure(stop + self._base)
            self._ysum[start:stop] += row * self.config.window
            self._wsum[start:stop] += self.config.window**2
            self._frames_seen += 1
            # Once frame m is in, absolute positions below (m+1)*hop cannot
            # be touched by any later frame.
            self._ready = (m + 1) * self._hop - self._base
        return self._ready

    def take_ready(self) -> np.ndarray:
        """Pop and return normalised samples that will not change anymore."""
        return self._drain(self._ready)

    def _drain(self, count: int) -> np.ndarray:
        if count <= 0:
            return np.empty(0, dtype=np.float64)
        eps = _weight_epsilon(self.config.window)
        good = self._wsum[:count] > eps
        y = np.zeros(count, dtype=np.float64)
        y[good] = self._ysum[:count][good] / self._wsum[:count][good]
        # Discard the fully covered prefix; the suffix [count:] still
        # overlaps frames already placed and future frames.
        self._ysum = self._ysum[count:]
        self._wsum = self._wsum[count:]
        self._base += count
        self._ready -= count
        self._drained.append(y)
        self._drained_len += count
        return y

    def _full_output(self, tail: np.ndarray) -> np.ndarray:
        """Prepend earlier take_ready() output to the final tail."""
        if self._drained_len == 0:
            return tail
        return np.concatenate(self._drained + [tail])

    def _signal_weight(self, covered: int) -> np.ndarray:
        # Weight array over absolute padded indices [0, covered).
        wsum = np.zeros(covered, dtype=np.float64)
        for j in range(self._frames_seen):
            s = j * self._hop
            wsum[s : s + self._n_fft] += self.config.window**2
        return wsum

    def finish(
        self, signal_length: int | None = None
    ) -> tuple[np.ndarray, dict]:
        """Flush all covered samples and (center) trim to signal length.

        Earlier samples popped by :meth:`take_ready` are reassembled
        internally, so this returns the same full result as the whole-signal
        iSTFT regardless of how many times ``take_ready`` was called.
        """
        n_fft, hop = self._n_fft, self._hop
        m = self._frames_seen
        covered = (m - 1) * hop + n_fft if m > 0 else 0
        # All remaining live samples are final; normalise in one pass.
        remaining = min(max(0, covered - self._base), self._wsum.size)
        tail = self._drain(remaining)
        padded = self._full_output(tail)  # absolute indices [0, covered)

        eps = _weight_epsilon(self.config.window)
        wsum = np.zeros(covered, dtype=np.float64)
        for j in range(m):
            s = j * hop
            wsum[s : s + n_fft] += self.config.window**2

        info: dict = {
            "n_frames": int(m),
            "buffer_length": int(covered),
        }

        if self.config.center:
            if signal_length is None:
                raise STFTError(
                    "signal_length is required for streaming finish with center=True"
                )
            cut = n_fft // 2
            wsig = wsum[cut : cut + signal_length]
            zeros = [int(i) for i in np.flatnonzero(wsig <= eps)]
            info["zero_weight_positions"] = zeros
            info["reconstruction_possible"] = len(zeros) == 0
            info["min_weight"] = float(wsig.min()) if wsig.size else 0.0
            info["signal_length"] = int(signal_length)
            return padded[cut : cut + signal_length], info

        region = wsum[: min(covered, padded.size)]
        zeros = [int(i) for i in np.flatnonzero(region <= eps)]
        info["zero_weight_positions"] = zeros
        info["reconstruction_possible"] = len(zeros) == 0
        info["min_weight"] = float(region.min()) if region.size else 0.0
        result = padded
        if signal_length is not None:
            result = result[:signal_length]
        return result, info
