"""Symbol handling.

Input epsilon and output epsilon are *distinct tape positions*.  Internally
both are represented by the empty string (``EPS``) but they are never
matched against each other in composition: an epsilon on the output tape of
the left transducer does not synchronise with an epsilon on the input tape
of the right transducer (see :mod:`wfst_service.core.compose`).

Fixtures may spell epsilon either as ``""`` or as the literal token
``"<eps>"``; the literal is normalised away at the spec boundary.
"""

from __future__ import annotations

EPS: str = ""
"""Canonical epsilon label."""

EPS_LITERAL: str = "<eps>"
"""Accepted spelling of epsilon in JSON corpora."""


def normalize_label(label: object) -> str:
    """Normalise one externally supplied label.

    Labels are single characters; epsilon is ``""`` / ``"<eps>"``.
    Raises :class:`TypeError` / :class:`ValueError` on malformed input so the
    spec layer can turn it into a structured validation error.
    """
    if not isinstance(label, str):
        raise TypeError(f"label must be a string, got {type(label).__name__}")
    if label == EPS_LITERAL:
        return EPS
    if label == EPS:
        return EPS
    if len(label) != 1:
        raise ValueError(
            f"labels must be single characters (or epsilon {EPS_LITERAL!r}), "
            f"got {label!r} (multi-character symbols are out of the declared "
            "symbol scope)"
        )
    if any(ch.isspace() for ch in label):
        raise ValueError(f"whitespace labels are not allowed, got {label!r}")
    return label


def is_epsilon(label: str) -> bool:
    return label == EPS


def render(label: str) -> str:
    """Render a label for log lines and error messages."""
    return EPS_LITERAL if label == EPS else label
