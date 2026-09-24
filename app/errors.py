"""Domain errors for DBC parsing and CAN frame decoding.

All errors carry a stable ``code`` so the HTTP layer can map them without
inspecting free-form message text.
"""

from __future__ import annotations


class DecoderError(Exception):
    """Base class for all errors raised by this package."""

    code = "decoder_error"


class DBCParseError(DecoderError):
    """A line of DBC text is malformed or uses an unsupported construct."""

    code = "dbc_parse_error"

    def __init__(self, message: str, line_no: int | None = None) -> None:
        if line_no is not None:
            message = f"line {line_no}: {message}"
        super().__init__(message)
        self.line_no = line_no


class DBCValidationError(DecoderError):
    """The DBC is grammatically parseable but semantically invalid.

    Examples: illegal bit width/length, signals overlapping inside the same
    multiplex branch, a signal reaching past the declared DLC.
    """

    code = "dbc_validation_error"


class FrameError(DecoderError):
    """A frame cannot be decoded against its DBC definition."""

    code = "frame_error"


class NotFoundError(DecoderError):
    """Referenced DBC / message does not exist."""

    code = "not_found"
