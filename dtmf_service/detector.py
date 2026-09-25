"""Frame-level dual-tone detection and digit-sequence decoding.

Pipeline per frame:
  1. Goertzel power at the 4 low-group and 4 high-group frequencies.
  2. Noise gate: frame RMS must exceed ``min_rms``.
  3. Dominance: strongest group member must exceed the runner-up by
     ``dominance_ratio`` (rejects speech-like broadband content).
  4. Energy ratio: the two winning tones must carry at least
     ``min_tone_energy_ratio`` of the frame energy (rejects noise).
  5. Twist: high/low power ratio must stay within
     [``twist_min_db``, ``twist_max_db``] (amplitude-ratio check).

Frames passing all checks vote for a key; the sequence decoder keeps
runs of at least ``min_tone_ms`` separated by gaps of at least
``min_gap_ms``. Anything failing a check is reported as a rejection
with a reason, never silently dropped.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from .goertzel import goertzel_power, tone_mean_square
from .synth import HIGH_FREQS, KEYPAD, LOW_FREQS

_FREQ_TO_KEY: dict[tuple[float, float], str] = {v: k for k, v in KEYPAD.items()}


@dataclass(frozen=True)
class DetectorConfig:
    sample_rate: int = 8000
    frame_ms: float = 40.0
    hop_ms: float = 20.0
    min_tone_ms: float = 40.0  # minimum key-press duration
    min_gap_ms: float = 30.0  # minimum silence between distinct presses
    min_rms: float = 0.01  # noise gate on frame RMS (full scale = 1.0)
    dominance_ratio: float = 2.0  # best/second-best power within a group
    min_tone_energy_ratio: float = 0.5  # two-tone energy / frame energy
    twist_min_db: float = -8.0  # high tone quieter than low by at most 8 dB
    twist_max_db: float = 4.0  # high tone louder than low by at most 4 dB

    @property
    def frame_len(self) -> int:
        return int(round(self.sample_rate * self.frame_ms / 1000.0))

    @property
    def hop_len(self) -> int:
        return int(round(self.sample_rate * self.hop_ms / 1000.0))


@dataclass(frozen=True)
class DigitEvent:
    key: str
    start_ms: float
    end_ms: float
    low_freq: float
    high_freq: float
    twist_db: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Rejection:
    reason: str
    start_ms: float
    end_ms: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DetectionResult:
    digits: str
    events: tuple[DigitEvent, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    sample_rate: int = 8000
    n_samples: int = 0

    def to_dict(self) -> dict:
        return {
            "digits": self.digits,
            "events": [e.to_dict() for e in self.events],
            "rejections": [r.to_dict() for r in self.rejections],
            "sample_rate": self.sample_rate,
            "n_samples": self.n_samples,
        }


@dataclass
class _FrameVote:
    key: str | None
    reason: str | None  # rejection reason when key is None
    low_freq: float = 0.0
    high_freq: float = 0.0
    twist_db: float = 0.0


class DTMFDetector:
    """Detect dual-tone key presses in a synthetic-audio signal."""

    def __init__(self, config: DetectorConfig | None = None) -> None:
        self.config = config or DetectorConfig()
        # Hann-window cache keyed by frame length. The window widens the
        # Goertzel mainlobe so tones with a few percent of frequency offset
        # still land inside it (a rectangular window loses ~half the power
        # at 0.6 bins of offset).
        self._windows: dict[int, tuple[np.ndarray, float, float]] = {}

    def _window_for(self, frame_len: int) -> tuple[np.ndarray, float, float]:
        if frame_len not in self._windows:
            window = np.hanning(frame_len)
            self._windows[frame_len] = (
                window,
                float(np.sum(window)),
                float(np.sum(window**2)),
            )
        return self._windows[frame_len]

    def detect(self, samples: np.ndarray, sample_rate: int | None = None) -> DetectionResult:
        """Decode a digit sequence from a mono float signal."""
        cfg = self._effective_config(sample_rate)
        x = np.asarray(samples, dtype=np.float64).ravel()
        votes = [
            self._classify_frame(x[start : start + cfg.frame_len], cfg)
            for start in range(0, max(0, len(x) - cfg.frame_len + 1), cfg.hop_len)
        ]
        events, rejections = self._decode(votes, cfg)
        return DetectionResult(
            digits="".join(e.key for e in events),
            events=tuple(events),
            rejections=tuple(rejections),
            sample_rate=cfg.sample_rate,
            n_samples=len(x),
        )

    # ------------------------------------------------------------------
    # Frame classification
    # ------------------------------------------------------------------

    def _effective_config(self, sample_rate: int | None) -> DetectorConfig:
        if sample_rate is None or sample_rate == self.config.sample_rate:
            return self.config
        return DetectorConfig(
            **{**asdict(self.config), "sample_rate": sample_rate}
        )

    def _classify_frame(self, frame: np.ndarray, cfg: DetectorConfig) -> _FrameVote:
        if frame.size < cfg.frame_len:
            return _FrameVote(None, "short_frame")
        rms = float(np.sqrt(np.mean(frame**2)))
        if rms < cfg.min_rms:
            return _FrameVote(None, "silence")

        window, window_sum, window_sq_sum = self._window_for(cfg.frame_len)
        wframe = frame * window
        low_powers = [goertzel_power(wframe, cfg.sample_rate, f) for f in LOW_FREQS]
        high_powers = [goertzel_power(wframe, cfg.sample_rate, f) for f in HIGH_FREQS]
        low_idx, low_ok = _pick_peak(low_powers, cfg.dominance_ratio)
        high_idx, high_ok = _pick_peak(high_powers, cfg.dominance_ratio)
        if not (low_ok and high_ok):
            return _FrameVote(None, "no_dominant_tone_pair")

        # Energies measured in the windowed domain: for a pure tone both
        # estimates below equal A**2/2, so their ratio is meaningful.
        frame_ms_energy = float(np.sum(wframe**2)) / window_sq_sum
        tone_energy = tone_mean_square(
            low_powers[low_idx], cfg.frame_len, window_sum
        ) + tone_mean_square(high_powers[high_idx], cfg.frame_len, window_sum)
        if tone_energy < cfg.min_tone_energy_ratio * frame_ms_energy:
            return _FrameVote(None, "tone_energy_below_noise")

        twist_db = 10.0 * np.log10(high_powers[high_idx] / low_powers[low_idx])
        if not cfg.twist_min_db <= twist_db <= cfg.twist_max_db:
            return _FrameVote(None, "twist_out_of_range")

        low_freq = LOW_FREQS[low_idx]
        high_freq = HIGH_FREQS[high_idx]
        return _FrameVote(
            _FREQ_TO_KEY[(low_freq, high_freq)], None, low_freq, high_freq, twist_db
        )

    # ------------------------------------------------------------------
    # Sequence decoding
    # ------------------------------------------------------------------

    def _decode(
        self, votes: list[_FrameVote], cfg: DetectorConfig
    ) -> tuple[list[DigitEvent], list[Rejection]]:
        events: list[DigitEvent] = []
        rejections: list[Rejection] = []
        min_tone_frames = max(1, int(np.ceil(cfg.min_tone_ms / cfg.hop_ms)))
        min_gap_frames = max(1, int(np.ceil(cfg.min_gap_ms / cfg.hop_ms)))

        i = 0
        n = len(votes)
        while i < n:
            vote = votes[i]
            if vote.key is None:
                j = _run_end(
                    votes, i, lambda v, r=vote.reason: v.key is None and v.reason == r
                )
                if vote.reason != "silence":
                    rejections.append(
                        Rejection(
                            vote.reason or "unknown",
                            _ms(i, cfg),
                            _ms(j, cfg),
                        )
                    )
                i = j
                continue
            j = _run_end(votes, i, lambda v, k=vote.key: v.key == k)
            if j - i >= min_tone_frames:
                events.append(
                    DigitEvent(
                        key=vote.key,
                        start_ms=_ms(i, cfg),
                        end_ms=_ms(j, cfg),
                        low_freq=vote.low_freq,
                        high_freq=vote.high_freq,
                        twist_db=round(vote.twist_db, 2),
                    )
                )
            else:
                rejections.append(
                    Rejection("tone_too_short", _ms(i, cfg), _ms(j, cfg))
                )
            i = j

        events = _merge_same_key(events, min_gap_frames, cfg)
        return events, rejections


def _pick_peak(powers: list[float], dominance_ratio: float) -> tuple[int, bool]:
    """Index of the strongest entry and whether it dominates the runner-up."""
    order = np.argsort(powers)[::-1]
    best, second = int(order[0]), float(powers[order[1]])
    ok = powers[best] >= dominance_ratio * max(second, 1e-30)
    return best, ok


def _run_end(votes: list[_FrameVote], start: int, pred) -> int:
    j = start
    while j < len(votes) and pred(votes[j]):
        j += 1
    return j


def _ms(frame_index: int, cfg: DetectorConfig) -> float:
    return round(frame_index * cfg.hop_ms, 1)


def _merge_same_key(
    events: list[DigitEvent], min_gap_frames: int, cfg: DetectorConfig
) -> list[DigitEvent]:
    """Merge adjacent events with the same key separated by a short gap.

    A real key re-press requires a silence of at least ``min_gap_ms``;
    shorter interruptions are treated as detection dropouts.
    """
    if not events:
        return events
    merged = [events[0]]
    for ev in events[1:]:
        prev = merged[-1]
        gap_ms = ev.start_ms - prev.end_ms
        if ev.key == prev.key and gap_ms < min_gap_frames * cfg.hop_ms:
            merged[-1] = DigitEvent(
                key=prev.key,
                start_ms=prev.start_ms,
                end_ms=ev.end_ms,
                low_freq=prev.low_freq,
                high_freq=prev.high_freq,
                twist_db=prev.twist_db,
            )
        else:
            merged.append(ev)
    return merged
