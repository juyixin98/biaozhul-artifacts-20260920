# Error hierarchy for the masking rule compiler.
#
# SECURITY NOTE (see README "安全说明"): exception messages MUST stay free of
# original sensitive values. They may mention rule ids, JSON paths, parameter
# names and type names, never the data found at a field.


class MaskCompilerError(Exception):
    """Base class for all maskcompiler errors."""


class RuleSyntaxError(MaskCompilerError):
    """A ruleset document is structurally invalid (rejected at compile time)."""


class UnknownTransformError(RuleSyntaxError):
    """A rule uses a transform name that is not in the registry."""


class RuleParameterError(RuleSyntaxError):
    """A transform parameter is missing, malformed or unsupported."""


class PathSyntaxError(RuleSyntaxError):
    """A path expression is not valid JSONPath-subset syntax."""


class RuleConflictError(MaskCompilerError):
    """Multiple same-priority rules remain applicable to the same location."""


class MissingFieldError(MaskCompilerError):
    """A rule with ``on_missing: error`` matched zero locations."""


class TypeMismatchError(MaskCompilerError):
    """A transform cannot be applied to the JSON type found at a location."""


class KeyManagerError(MaskCompilerError):
    """A key file is missing, malformed, has bad permissions or a bad version."""
