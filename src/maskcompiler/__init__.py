"""maskcompiler -- structured JSON data-masking rule compiler."""

from .compiler import CompiledRuleset, RuleEntry, compile_ruleset
from .engine import ApplyReport, MaskResult, apply_ruleset
from .errors import (
    KeyManagerError,
    MaskCompilerError,
    MissingFieldError,
    PathSyntaxError,
    RuleConflictError,
    RuleParameterError,
    RuleSyntaxError,
    TypeMismatchError,
    UnknownTransformError,
)
from .keys import (
    KeyBundle,
    bundle_from_dict,
    bundle_to_dict,
    generate_bundle,
    load_keyfile,
    save_keyfile,
)
from .paths import parse_path
from .transforms import supported_transforms

__version__ = "0.1.0"

__all__ = [
    "ApplyReport",
    "CompiledRuleset",
    "KeyBundle",
    "KeyManagerError",
    "MaskCompilerError",
    "MaskResult",
    "MissingFieldError",
    "PathSyntaxError",
    "RuleConflictError",
    "RuleEntry",
    "RuleParameterError",
    "RuleSyntaxError",
    "TypeMismatchError",
    "UnknownTransformError",
    "apply_ruleset",
    "bundle_from_dict",
    "bundle_to_dict",
    "compile_ruleset",
    "generate_bundle",
    "load_keyfile",
    "parse_path",
    "save_keyfile",
    "supported_transforms",
]
