"""Online chunked STFT / iSTFT with bounded memory.

The streamers reproduce :func:`streaming_stft.stft.build_grid` exactly:
feeding a signal in arbitrary chunk sizes produces the same frames and
reconstruction as the bulk transform (frame contributions are summed in
frame order on both paths).

Analysis (:class:`STFTStreamer`)
    Push arbitrary-length input blocks; consume complete frames as rFFT
    spectra.  ``flush()`` appends the deterministic right padding
    (``nfft//2`` mode-dependent samples plus zero fill to the last frame
    end) and yields the tail frames.

Synthesis (:class:`ISTFTStreamer`)
    Push spectra in frame order; consume signal samples as soon as all
    frames covering them have been received.  The hop-region produced by
    the most recently pushed frame is held back by one push: it is only
    releasable without knowing the total signal length once another frame
    proves it was not the last frame.  ``flush(signal_length)`` releases the
    held-back region and the final tail, masked to the declared length.

Ring invariant
    The synthesis ring always writes the next frame at slot 0 and slides by
    ``hop``; its length is exactly ``nfft`` plus a pending chunk of at most
    ``hop`` samples.
"""

from __future__ import annotations

import numpy as np

from .stft import STFTConfig, frame_plan, validate_config

__all__ = ["STFTStreamer", "ISTFTStreamer"]

_WEIGHT_TOL = 1e-12


class STFTStreamer:
    """Frame a stream of samples into windowed rFFT frames."""

    def __init__(self, config: STFTConfig | None = None) -> None:
        self.config = config or STFTConfig()
        validate_config(self.config)
        self._window = self.config.resolved_window()
        self._n_bins = self.config.nfft // 2 + 1
        self._pad_left = self.config.nfft // 2 if self.config.center else 0

        self._buf = np.zeros(0, dtype=np.float64)  # grid suffix, un-emitted
        self._base = 0  # grid index of self._buf[0]
        self._frame_index = 0
        self._input_total = 0
        # Raw input prefix until the left pad is seeded, then the last
        # pad_left + 1 samples (needed for the right reflect pad).
        self._raw = np.zeros(0, dtype=np.float64)
        self._seeded = not self.config.center
        self._finished = False

    @property
    def frame_index(self) -> int:
        """Index of the next frame that will be emitted."""
        return self._frame_index

    @property
    def is_finished(self) -> bool:
        return self._finished

    def _empty_spectra(self) -> np.ndarray:
        return np.zeros((0, self._n_bins), dtype=np.complex128)

    def _seed_left_padding(self) -> None:
        """Prepend the deterministic left pad once enough input is available."""
        p = self._pad_left
        if p == 0:
            self._seeded = True
            return
        if self.config.pad_mode == "constant":
            left = np.zeros(p)
        elif self.config.pad_mode == "edge":
            if self._raw.shape[0] < 1:
                return
            left = np.full(p, self._raw[0])
        else:  # reflect needs p + 1 raw samples
            if self._raw.shape[0] <= p:
                return
            left = np.pad(self._raw[: p + 1], (p, 0), mode="reflect")[:p]
        self._buf = np.concatenate([left, self._raw])
        self._base = 0
        self._seeded = True

    def push(self, samples: np.ndarray) -> np.ndarray:
        """Add an input block; return newly complete frame spectra ``(k, bins)``."""
        if self._finished:
            raise RuntimeError("push() called after flush()")
        block = np.asarray(samples, dtype=np.float64).reshape(-1)
        self._input_total += block.shape[0]
        if self._pad_left > 0:
            self._raw = np.concatenate([self._raw, block])
        if not self._seeded:
            self._seed_left_padding()
            if not self._seeded:
                return self._empty_spectra()
            # Raw prefix has been consumed into buf; only the tail is needed.
            self._raw = self._raw[-(self._pad_left + 1) :]
        else:
            self._buf = np.concatenate([self._buf, block])
            if self._pad_left > 0:
                self._raw = self._raw[-(self._pad_left + 1) :]
        return self._extract_frames()

    def _extract_frames(self) -> np.ndarray:
        frames: list[np.ndarray] = []
        while True:
            start = self._frame_index * self.config.hop
            end = start + self.config.nfft
            if end > self._base + self._buf.shape[0]:
                break
            frame = self._buf[start - self._base : end - self._base]
            frames.append(np.fft.rfft(frame * self._window, n=self.config.nfft))
            self._frame_index += 1
        keep_from = self._frame_index * self.config.hop - self._base
        if keep_from > 0:
            self._buf = self._buf[keep_from:]
            self._base += keep_from
        if not frames:
            return self._empty_spectra()
        return np.stack(frames)

    def flush(self) -> np.ndarray:
        """Append right padding per the shared grid and emit tail frames."""
        if self._finished:
            raise RuntimeError("flush() called twice")
        self._finished = True
        n = self._input_total
        p = self._pad_left

        if n == 0:
            return self._empty_spectra()
        if not self._seeded:
            raise ValueError(
                f"reflect padding needs more than {p} samples (nfft//2), "
                f"got {n}; use pad_mode='edge' or 'constant'"
            )

        base_length, n_frames, grid_length = frame_plan(self.config, n)
        if n_frames == 0:
            return self._empty_spectra()  # short input: zero frames by design

        current_end = self._base + self._buf.shape[0]  # == p + n (center)
        extra = grid_length - current_end

        pieces: list[np.ndarray] = []
        if self.config.center and extra > 0:
            # The buffer always ends exactly at core end (p + n), so the
            # mode-dependent extension starts with its first sample here.
            mode_fill = min(p, extra)
            if self.config.pad_mode == "constant":
                pieces.append(np.zeros(mode_fill))
            elif self.config.pad_mode == "edge":
                pieces.append(np.full(mode_fill, self._raw[-1]))
            else:
                seed = self._raw[-(p + 1) :]
                # reflect over the boundary seed[-1] == x[n-1]:
                # x[n-2], x[n-3], ..., x[n-1-p]
                extension = seed[:p][::-1]
                pieces.append(extension[:mode_fill])
        zero_fill = extra - sum(piece.shape[0] for piece in pieces)
        if zero_fill > 0:
            pieces.append(np.zeros(zero_fill))
        if pieces:
            self._buf = np.concatenate([self._buf, *pieces])
        return self._extract_frames()


class ISTFTStreamer:
    """Streaming inverse STFT with windowed overlap-add normalisation.

    On each pushed frame the ring slides by ``hop``; the normalized hop
    chunk is held in ``_pending`` and only handed out when the next frame
    arrives (proving the chunk lies inside the signal) or at
    :meth:`flush`, where it is length-masked.  This keeps memory bounded
    to ``nfft + hop`` samples without needing the signal length up front.
    """

    def __init__(self, config: STFTConfig | None = None) -> None:
        self.config = config or STFTConfig()
        validate_config(self.config)
        self._window = self.config.resolved_window()
        self._w2 = self._window * self._window
        self._pad_left = self.config.nfft // 2 if self.config.center else 0

        self._acc = np.zeros(self.config.nfft, dtype=np.float64)
        self._wsum = np.zeros(self.config.nfft, dtype=np.float64)
        self._base = 0  # grid index of ring slot 0
        self._next_frame = 0
        self._emitted = 0
        self._pending_values = np.zeros(0, dtype=np.float64)
        self._pending_positions = np.zeros(0, dtype=np.int64)
        self._signal_length: int | None = None
        self._zero_weight: list[int] = []
        self._weight_log: list[np.ndarray] = []
        self._finished = False

    @property
    def samples_emitted(self) -> int:
        return self._emitted

    @property
    def is_finished(self) -> bool:
        return self._finished

    def _normalize_slot(self, slot: int) -> tuple[float, float, int]:
        position = self._base + slot
        weight = float(self._wsum[slot])
        if weight > _WEIGHT_TOL:
            value = float(self._acc[slot] / weight)
        else:
            value = 0.0
            if position >= self._pad_left:
                self._zero_weight.append(position - self._pad_left)
        return value, weight, position

    def _slide(self) -> None:
        """Normalize ring slots ``[0, hop)`` into the held-back chunk.

        The previously held chunk is NOT changed here; the caller releases
        it because a new frame just proved it lies inside the signal.
        """
        hop = self.config.hop
        values = np.empty(hop, dtype=np.float64)
        weights = np.empty(hop, dtype=np.float64)
        positions = np.empty(hop, dtype=np.int64)
        for slot in range(hop):
            values[slot], weights[slot], positions[slot] = self._normalize_slot(slot)
        keep = positions >= self._pad_left
        self._pending_values = values[keep]
        self._pending_positions = positions[keep]
        self._weight_log.append(weights[keep])

        self._acc[: self.config.nfft - hop] = self._acc[hop:]
        self._wsum[: self.config.nfft - hop] = self._wsum[hop:]
        self._acc[self.config.nfft - hop :] = 0.0
        self._wsum[self.config.nfft - hop :] = 0.0
        self._base += hop

    def push_frame(self, spectrum: np.ndarray) -> np.ndarray:
        """Add one frame spectrum; return the chunk finalized by this frame.

        Pushing frame *k* releases grid positions
        ``[(k-1)*hop, k*hop)`` (core samples only); the region the new
        frame shares with the unknown future is held until the next push.
        """
        if self._finished:
            raise RuntimeError("push_frame() called after flush()")
        if self._next_frame * self.config.hop != self._base:
            raise RuntimeError("streamer ring is out of alignment")
        released_values = self._pending_values
        self._pending_values = np.zeros(0, dtype=np.float64)

        frame = np.fft.irfft(np.asarray(spectrum), n=self.config.nfft)
        self._acc += frame * self._window
        self._wsum += self._w2
        self._next_frame += 1
        self._slide()
        self._emitted += int(released_values.size)
        return released_values

    def push_frames(self, spectra: np.ndarray) -> np.ndarray:
        """Add many frames; return the concatenated finalized samples."""
        chunks = [self.push_frame(spec) for spec in spectra]
        chunks = [chunk for chunk in chunks if chunk.size]
        if not chunks:
            return np.zeros(0, dtype=np.float64)
        return np.concatenate(chunks)

    def flush(self, signal_length: int) -> np.ndarray:
        """Release the held-back chunk and tail; ``signal_length`` samples total."""
        if self._finished:
            raise RuntimeError("flush() called twice")
        self._finished = True
        self._signal_length = int(signal_length)
        signal_end = self._pad_left + signal_length

        if self._next_frame == 0:
            # No frames exist (signal shorter than one window): every sample
            # has zero weight, mirroring the bulk istft result.
            self._zero_weight.extend(range(signal_length))
            self._emitted = signal_length
            return np.zeros(signal_length, dtype=np.float64)

        out: list[np.ndarray] = []
        held_mask = self._pending_positions < signal_end
        out.append(self._pending_values[held_mask])
        self._emitted += int(held_mask.sum())
        self._pending_values = np.zeros(0, dtype=np.float64)
        self._pending_positions = self._pending_positions[~held_mask]

        grid_end = (self._next_frame - 1) * self.config.hop + self.config.nfft
        tail_slots = min(grid_end - self._base, self.config.nfft)
        tail: list[float] = []
        tail_weights: list[float] = []
        for slot in range(tail_slots):
            position = self._base + slot
            if position >= signal_end:
                break
            value, weight, _ = self._normalize_slot(slot)
            if position >= self._pad_left:
                tail.append(value)
                tail_weights.append(weight)
        if tail:
            out.append(np.asarray(tail, dtype=np.float64))
            self._weight_log.append(np.asarray(tail_weights))
            self._emitted += len(tail)

        result = np.concatenate(out) if out else np.zeros(0)
        if self._emitted != signal_length:
            raise RuntimeError(
                f"streaming reconstruction emitted {self._emitted} samples, "
                f"expected {signal_length}"
            )
        return result

    @property
    def zero_weight_positions(self) -> np.ndarray:
        """Signal indices whose accumulated squared-window weight was zero."""
        limit = getattr(self, "_signal_length", None)
        indices = sorted(set(self._zero_weight))
        if limit is not None:
            indices = [i for i in indices if 0 <= i < limit]
        return np.asarray(indices, dtype=np.int64)

    @property
    def emitted_weights(self) -> np.ndarray:
        """Per-sample squared-window weights in emission order."""
        if not self._weight_log:
            return np.zeros(0)
        return np.concatenate(self._weight_log)
