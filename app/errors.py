"""Domain-specific errors mapped to HTTP responses in :mod:`app.main`.

No exception raised inside the interpreter ever executes policy-supplied
data as code; structural / type problems in the policy or request are
reported with a stable machine-readable ``code``.
"""


class PolicyError(Exception):
    """Base class for all interpreter errors."""

    code = "policy_error"
    http_status = 400

    def __init__(self, message: str, *, path: str | None = None):
        super().__init__(message)
        self.message = message
        # JSON-ish location inside the policy/expression, e.g. "rules[2].when".
        self.path = path

    def to_dict(self) -> dict:
        d = {"error": self.code, "message": self.message}
        if self.path is not None:
            d["path"] = self.path
        return d


class PolicyValidationError(PolicyError):
    """The policy document / expression AST is malformed."""

    code = "invalid_policy"
    http_status = 422


class EvaluationError(PolicyError):
    """The AST is well formed but cannot be evaluated (e.g. type clash)."""

    code = "evaluation_error"
    http_status = 422


class TrustError(PolicyError):
    """A signed policy bundle failed cryptographic verification."""

    code = "untrusted_policy"
    http_status = 403


class PayloadError(PolicyError):
    """The request body is not acceptable (bad shape / size / parameter)."""

    code = "invalid_request"
    http_status = 400
