"""Masking transforms.

Four transforms, all built on established primitives -- no home-grown crypto:

* ``mask``    keep a prefix/suffix (by Unicode code point), replace the middle
* ``redact``  drop the value (or replace the whole subtree with a fixed value)
* ``hash``    keyed HMAC-SHA256/384/512 pseudonymization (deterministic)
* ``encrypt`` reversible authenticated encryption via Fernet (AES-128-CBC +
              HMAC-SHA256); output is a new UTF-8 string

Only the parameter names listed per transform are accepted: unknown keys are
rejected (default-deny). Unknown transform names are rejected too.
"""

import base64
from typing import Any, Dict, Optional, Type

from cryptography.hazmat.primitives import hashes, hmac

from .errors import RuleParameterError, UnknownTransformError
from .keys import KeyBundle
from .logsafe import register_secret

_DIGESTS = {
    "sha256": hashes.SHA256,
    "sha384": hashes.SHA384,
    "sha512": hashes.SHA512,
}


class Transform:
    #: accepted parameter names; anything else is rejected
    param_names: frozenset = frozenset()
    #: whether the transform may replace a container (dict/list) node
    handles_containers = False

    def apply_scalar(self, value: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def apply_node(self, value: Any) -> Any:  # pragma: no cover - interface
        raise NotImplementedError


def _check_params(rule_id: str, params: Dict[str, Any], allowed: frozenset) -> None:
    extra = sorted(set(params) - allowed)
    if extra:
        raise RuleParameterError(
            "rule '%s': unknown parameter(s) %s" % (rule_id, ", ".join(extra))
        )


def _non_negative_int(rule_id: str, params: Dict[str, Any], key: str, default: int) -> int:
    value = params.get(key, default)
    # bool is a subclass of int -- reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuleParameterError("rule '%s': parameter '%s' must be a non-negative integer" % (rule_id, key))
    return value


class MaskTransform(Transform):
    param_names = frozenset({"keep_first", "keep_last", "mask_char", "mask_length"})

    def __init__(self, rule_id: str, params: Dict[str, Any]) -> None:
        _check_params(rule_id, params, self.param_names)
        self.keep_first = _non_negative_int(rule_id, params, "keep_first", 0)
        self.keep_last = _non_negative_int(rule_id, params, "keep_last", 0)
        self.mask_length: Optional[int] = None
        if "mask_length" in params:
            self.mask_length = _non_negative_int(rule_id, params, "mask_length", 0)
        mask_char = params.get("mask_char", "*")
        if not isinstance(mask_char, str) or len(mask_char) != 1:
            raise RuleParameterError("rule '%s': 'mask_char' must be a single Unicode character" % rule_id)
        self.mask_char = mask_char

    def apply_scalar(self, value: str) -> str:
        register_secret(value)
        n = len(value)
        kf, kl = self.keep_first, self.keep_last
        if kf + kl >= n:
            # Kept prefix and suffix would meet or overlap: hide everything.
            return self.mask_char * n
        middle = self.mask_length if self.mask_length is not None else n - kf - kl
        return value[:kf] + self.mask_char * middle + value[n - kl:]


class RedactTransform(Transform):
    param_names = frozenset({"replacement", "container"})
    handles_containers = True

    def __init__(self, rule_id: str, params: Dict[str, Any]) -> None:
        _check_params(rule_id, params, self.param_names)
        self.replacement = params["replacement"] if "replacement" in params else None
        container = params.get("container", "recurse")
        if container not in ("recurse", "replace"):
            raise RuleParameterError(
                "rule '%s': 'container' must be 'recurse' or 'replace'" % rule_id
            )
        self.container_mode = container

    def apply_node(self, value: Any) -> Any:
        _register_subtree(value)
        return self.replacement


class HashTransform(Transform):
    param_names = frozenset({"algo", "encoding", "prefix"})

    def __init__(self, rule_id: str, params: Dict[str, Any]) -> None:
        _check_params(rule_id, params, self.param_names)
        algo = params.get("algo", "sha256")
        if algo not in _DIGESTS:
            raise RuleParameterError(
                "rule '%s': unknown algo '%s' (supported: %s)"
                % (rule_id, algo, ", ".join(sorted(_DIGESTS)))
            )
        self.digest = _DIGESTS[algo]
        encoding = params.get("encoding", "hex")
        if encoding not in ("hex", "base64"):
            raise RuleParameterError("rule '%s': 'encoding' must be 'hex' or 'base64'" % rule_id)
        self.encoding = encoding
        prefix = params.get("prefix", "")
        if not isinstance(prefix, str):
            raise RuleParameterError("rule '%s': 'prefix' must be a string" % rule_id)
        self.prefix = prefix
        self._key: Optional[bytes] = None

    def bind_keys(self, bundle: KeyBundle) -> None:
        self._key = bundle.hmac_key

    def apply_scalar(self, value: str) -> str:
        register_secret(value)
        if self._key is None:
            raise RuleParameterError("'hash' transform requires an HMAC key (use --key-file)")
        signer = hmac.HMAC(self._key, self.digest())
        signer.update(value.encode("utf-8"))
        digest = signer.finalize()
        if self.encoding == "hex":
            encoded = digest.hex()
        else:
            encoded = base64.b64encode(digest).decode("ascii")
        return self.prefix + encoded


class EncryptTransform(Transform):
    param_names: frozenset = frozenset()

    def __init__(self, rule_id: str, params: Dict[str, Any]) -> None:
        _check_params(rule_id, params, self.param_names)
        self._fernet = None

    def bind_keys(self, bundle: KeyBundle) -> None:
        self._fernet = bundle.fernet()

    def apply_scalar(self, value: str) -> str:
        register_secret(value)
        if self._fernet is None:
            raise RuleParameterError("'encrypt' transform requires a key file (use --key-file)")
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")


_REGISTRY: Dict[str, Type[Transform]] = {
    "mask": MaskTransform,
    "redact": RedactTransform,
    "hash": HashTransform,
    "encrypt": EncryptTransform,
}


def supported_transforms() -> list:
    return sorted(_REGISTRY)


def create_transform(rule_id: str, name: str, params: Dict[str, Any]) -> Transform:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise UnknownTransformError(
            "rule '%s': unknown transform '%s' (supported: %s)"
            % (rule_id, name, ", ".join(supported_transforms()))
        )
    return cls(rule_id, params)


def _register_subtree(node: Any) -> None:
    """Register every string inside a redacted subtree."""
    if isinstance(node, str):
        register_secret(node)
    elif isinstance(node, dict):
        for v in node.values():
            _register_subtree(v)
    elif isinstance(node, list):
        for v in node:
            _register_subtree(v)
