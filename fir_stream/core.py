"""Core streaming FIR engine.

Design notes
------------
* Each channel is a :class:`StreamingFIR` holding one or more *lanes*.
  A lane is one FIR filter plus its own input-history state and a scalar
  weight.  The channel output is the weighted sum of lane outputs.
* A filter switch appends a new lane (primed with recent input history so
  its convolution reflects the actual past signal) and ramps its weight
  0 -> 1 with a raised-cosine crossfade while all previous lanes are
  scaled by (1 - ramp).  When the fade completes the old lanes are
  dropped, so steady-state cost is one convolution per block.
* Requesting a new switch while a fade is in progress simply appends
  another lane: the current weighted mix fades as a whole toward the new
  filter.  This makes "switch during switch" well defined.
* Empty blocks return an empty array and touch no state.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Cap on the retained input history used to prime new lanes.  Filters
# longer than this still work; their state is zero-padded on the left.
MAX_HISTORY_SAMPLES = 65536


def _validate_impulse_response(h: np.ndarray) -> np.ndarray:
    h = np.asarray(h, dtype=np.float64)
    if h.ndim != 1 or h.size == 0:
        raise ValueError("impulse response must be a non-empty 1-D array")
    if not np.all(np.isfinite(h)):
        raise ValueError("impulse response must contain only finite values")
    return h


def _raised_cosine_ramp(positions: np.ndarray, total: int) -> np.ndarray:
    """Smooth 0->1 ramp; positions are 1-based sample indices in the fade.
    Positions past ``total`` stay pinned at 1 (the cosine alone would
    oscillate back down)."""
    clamped = np.minimum(positions, total)
    ramp = 0.5 - 0.5 * np.cos(np.pi * clamped / total)
    return np.clip(ramp, 0.0, 1.0)


@dataclass
class _Lane:
    h: np.ndarray
    state: np.ndarray  # last len(h) - 1 input samples seen by this lane
    weight: float


def _convolve_block(lane: _Lane, x: np.ndarray) -> np.ndarray:
    """Convolve one block, advancing the lane state (mutable on purpose:
    the lane *is* the streaming state)."""
    m = lane.h.size
    if m == 1:
        return lane.h[0] * x
    buf = np.concatenate([lane.state, x])
    y = np.convolve(buf, lane.h)[m - 1 : m - 1 + x.size]
    lane.state = buf[-(m - 1):].copy()
    return y


class StreamingFIR:
    """Single-channel streaming FIR with crossfaded filter switching."""

    def __init__(self, h: np.ndarray):
        h = _validate_impulse_response(h)
        self._lanes = [_Lane(h=h, state=np.zeros(h.size - 1), weight=1.0)]
        self._history = np.zeros(0, dtype=np.float64)
        self._fade_total = 0
        self._fade_pos = 0

    # ------------------------------------------------------------------
    # introspection (used by tests and the service report)
    # ------------------------------------------------------------------
    @property
    def lane_weights(self) -> tuple[float, ...]:
        return tuple(lane.weight for lane in self._lanes)

    @property
    def num_lanes(self) -> int:
        return len(self._lanes)

    @property
    def current_filter(self) -> np.ndarray:
        """Impulse response of the most recently requested filter."""
        return self._lanes[-1].h.copy()

    @property
    def fade_in_progress(self) -> bool:
        return self._fade_total > 0

    def snapshot_state(self) -> list[np.ndarray]:
        """Copy of all lane states, for state-isolation assertions."""
        return [lane.state.copy() for lane in self._lanes]

    # ------------------------------------------------------------------
    # filter switching
    # ------------------------------------------------------------------
    def _prime_state(self, h: np.ndarray) -> np.ndarray:
        m = h.size - 1
        if m == 0:
            return np.zeros(0, dtype=np.float64)
        hist = self._history[-m:] if self._history.size else np.zeros(0)
        pad = np.zeros(m - hist.size, dtype=np.float64)
        return np.concatenate([pad, hist])

    def switch(self, h_new: np.ndarray, fade_samples: int) -> None:
        """Switch to ``h_new``.  ``fade_samples`` > 0 crossfades with a
        raised-cosine ramp; 0 performs an immediate switch (the new lane
        is still primed with input history, so the filter itself does not
        restart from silence)."""
        h_new = _validate_impulse_response(h_new)
        fade_samples = int(fade_samples)
        if fade_samples <= 0:
            self._lanes = [_Lane(h=h_new, state=self._prime_state(h_new), weight=1.0)]
            self._fade_total = 0
            self._fade_pos = 0
            return
        # Drop lanes that no longer contribute, then append the new one.
        self._lanes = [lane for lane in self._lanes if lane.weight > 0.0]
        self._lanes.append(_Lane(h=h_new, state=self._prime_state(h_new), weight=0.0))
        self._fade_total = fade_samples
        self._fade_pos = 0

    # ------------------------------------------------------------------
    # processing
    # ------------------------------------------------------------------
    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        if x.ndim != 1:
            raise ValueError("process expects a 1-D block")
        n = x.size
        if n == 0:
            # Empty block: no output, no state change.
            return np.zeros(0, dtype=np.float64)

        self._history = np.concatenate([self._history, x])[-MAX_HISTORY_SAMPLES:]

        if self._fade_total == 0:
            out = np.zeros(n, dtype=np.float64)
            for lane in self._lanes:
                out += lane.weight * _convolve_block(lane, x)
            return out

        # Fade active: newest lane ramps up, all older lanes ramp down.
        positions = self._fade_pos + np.arange(1, n + 1)
        w = _raised_cosine_ramp(positions, self._fade_total)
        out = np.zeros(n, dtype=np.float64)
        for lane in self._lanes[:-1]:
            out += (lane.weight * (1.0 - w)) * _convolve_block(lane, x)
        new_lane = self._lanes[-1]
        out += w * _convolve_block(new_lane, x)

        w_end = float(w[-1])
        for lane in self._lanes[:-1]:
            lane.weight *= 1.0 - w_end
        new_lane.weight = w_end
        self._fade_pos += n

        if self._fade_pos >= self._fade_total:
            # Fade finished inside this block: keep only the new lane.
            self._lanes = [_Lane(h=new_lane.h, state=new_lane.state, weight=1.0)]
            self._fade_total = 0
            self._fade_pos = 0
        return out


class MultiChannelFIR:
    """Fixed-channel-count bank of independent :class:`StreamingFIR`s.

    Channels are fully isolated: each owns its lanes, states and fade
    schedule.  Changing the channel count reconfigures the bank and
    resets all state (documented behavior — state is not migrated).
    """

    def __init__(self, num_channels: int, h: np.ndarray):
        if int(num_channels) < 1:
            raise ValueError("num_channels must be >= 1")
        self._base_filter = _validate_impulse_response(h)
        self._channels = [StreamingFIR(self._base_filter) for _ in range(int(num_channels))]

    @property
    def num_channels(self) -> int:
        return len(self._channels)

    def channel(self, index: int) -> StreamingFIR:
        return self._channels[index]

    def set_num_channels(self, num_channels: int) -> None:
        """Reconfigure the bank.  All filter state is reset; the current
        per-channel filter of channel 0 is reused as the base filter."""
        if int(num_channels) < 1:
            raise ValueError("num_channels must be >= 1")
        self._base_filter = self._channels[0].current_filter
        self._channels = [StreamingFIR(self._base_filter) for _ in range(int(num_channels))]

    def switch(self, h_new: np.ndarray, fade_samples: int, channel: int | None = None) -> None:
        """Switch one channel (``channel`` index) or all channels (None)."""
        if channel is None:
            for ch in self._channels:
                ch.switch(h_new, fade_samples)
        else:
            self._channels[channel].switch(h_new, fade_samples)

    def process(self, block: np.ndarray) -> np.ndarray:
        """Process a ``(num_channels, num_samples)`` block.

        A block with zero samples returns a ``(num_channels, 0)`` array
        and leaves every channel's state untouched.
        """
        block = np.asarray(block, dtype=np.float64)
        if block.ndim != 2:
            raise ValueError("block must be 2-D: (channels, samples)")
        if block.shape[0] != len(self._channels):
            raise ValueError(
                f"expected {len(self._channels)} channels, got {block.shape[0]}"
            )
        if block.shape[1] == 0:
            return np.zeros((len(self._channels), 0), dtype=np.float64)
        return np.vstack([ch.process(row) for ch, row in zip(self._channels, block)])
