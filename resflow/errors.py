"""Shared error types for the ResFlow toolchain.

All compiler front-end failures raise :class:`ResFlowError` carrying a
:class:`~resflow.lexer.Location` so the JSON service can report exact source
positions.
"""


class ResFlowError(Exception):
    """Base class for lexical, syntactic and static-check failures."""

    def __init__(self, message, location):
        super().__init__(message)
        self.message = message
        self.location = location

    def to_dict(self):
        return {"message": self.message, "location": self.location.to_dict()}
