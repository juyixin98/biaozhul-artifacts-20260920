"""Error hierarchy for the interval-AI language toolchain.

Every error carries an optional source location (1-based line/column plus a
half-open character span) and a machine readable ``code`` so that the JSON
service can report structured diagnostics.
"""


class IvalError(Exception):
    """Base class for all toolchain errors."""

    code = "internal_error"

    def __init__(self, message, loc=None):
        super().__init__(message)
        self.message = message
        self.loc = loc

    def to_dict(self):
        d = {"error": self.code, "message": self.message}
        if self.loc is not None:
            d["loc"] = self.loc.to_dict()
        return d


class LexError(IvalError):
    code = "lex_error"


class ParseError(IvalError):
    code = "parse_error"


class SemanticError(IvalError):
    code = "semantic_error"


class ConcreteExecError(IvalError):
    """Raised by the reference interpreter when a program goes wrong.

    ``kind`` is one of ``div_by_zero``, ``index_out_of_bounds`` (negative
    index also maps here), ``stack_overflow`` / ``step_limit`` (non
    termination guard), ``uninitialized``.
    """

    code = "execution_error"

    def __init__(self, kind, message, loc=None):
        super().__init__(message, loc)
        self.kind = kind

    def to_dict(self):
        d = super().to_dict()
        d["kind"] = self.kind
        return d


class ServiceError(IvalError):
    code = "invalid_request"
