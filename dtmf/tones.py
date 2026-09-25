"""双音（DTMF 风格）频率表与键位映射。"""

from __future__ import annotations

ROW_FREQS: tuple[float, ...] = (697.0, 770.0, 852.0, 941.0)
COL_FREQS: tuple[float, ...] = (1209.0, 1336.0, 1477.0, 1633.0)
ALL_FREQS: tuple[float, ...] = ROW_FREQS + COL_FREQS

KEYPAD: tuple[tuple[str, ...], ...] = (
    ("1", "2", "3", "A"),
    ("4", "5", "6", "B"),
    ("7", "8", "9", "C"),
    ("*", "0", "#", "D"),
)

KEY_TO_FREQS: dict[str, tuple[float, float]] = {
    key: (ROW_FREQS[r], COL_FREQS[c])
    for r, row in enumerate(KEYPAD)
    for c, key in enumerate(row)
}

VALID_KEYS: tuple[str, ...] = tuple(KEY_TO_FREQS.keys())
