"""The policy expression interpreter.

Security model
--------------
* Conditions are **declarative ASTs** (plain JSON-serializable dicts), never
  strings. There is no ``eval``/``exec``/``compile`` anywhere in the project;
  untrusted policy input can only select an operator from a fixed whitelist.
* Values come from an :class:`AttrBag` split into ``subject`` and ``resource``
  namespaces. A missing attribute yields :data:`~app.logic.Tri.UNKNOWN`.
* Typed comparisons reject mismatched types with :class:`EvaluationError`
  (HTTP 422) — equality simply returns FALSE — so malformed requests fail
  loudly instead of silently allowing.

AST node reference
~~~~~~~~~~~~~~~~~~
``{"op": "lit",   "value": <json literal>}``
``{"op": "attr",  "bag": "subject"|"resource", "key": "<attr name>"}``
``{"op": "exists", "bag": ..., "key": ...}``
``{"op": "not"|"and"|"or", "args": [<node>, ...]}``
``{"op": "eq"|"ne"|"lt"|"lte"|"gt"|"gte"|"in"|"contains"|"subset",
   "left": <node>, "right": <node>}``

Allowed leaf values: string, number (int/float, excluding bool), bool,
null, and homogeneous lists of those.
"""

from __future__ import annotations

from numbers import Real
from typing import Any

from .errors import EvaluationError, PolicyValidationError
from .logic import Tri, tri_and, tri_not, tri_or

# ---------------------------------------------------------------------------
# Whitelists
# ---------------------------------------------------------------------------

#: Unary / n-ary logical operators (the number of args is checked per op).
_LOGICAL_OPS = {"not", "and", "or"}
#: Binary comparison / set operators.
_BINARY_OPS = {"eq", "ne", "lt", "lte", "gt", "gte", "in", "contains", "subset"}
#: Every recognised op (nothing outside this set is ever dispatched).
ALL_OPS = {"lit", "attr", "exists"} | _LOGICAL_OPS | _BINARY_OPS

BAGS = ("subject", "resource")

# Sentinel meaning "attribute absent from the bag". Never leaks to callers:
# attribute resolution converts it into the appropriate UNKNOWN behaviour.
_MISSING = object()


# ---------------------------------------------------------------------------
# Attribute bags
# ---------------------------------------------------------------------------

class AttrBag:
    """A read-only namespace of subject or resource attributes."""

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any] | None):
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise EvaluationError("attribute bag must be a JSON object")
        self._data = data

    def get(self, key: str) -> Any:
        return self._data.get(key, _MISSING)

    def contains_key(self, key: str) -> bool:
        return key in self._data

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"AttrBag({self._data!r})"


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def _family(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list"
    return "unsupported"


def is_literal(v: Any) -> bool:
    """Whether ``v`` is an admissible JSON leaf/list value (recursive)."""
    if v is None or isinstance(v, (bool, str, int, float)):
        return True
    if isinstance(v, list):
        return all(is_literal(x) for x in v)
    return False


def _values_equal(a: Any, b: Any) -> bool:
    """Family-aware equality.

    Cross-family values are unequal (``1`` vs ``"1"``, ``True`` vs ``1``);
    lists compare element-wise. Missing operands are handled by the caller.
    """
    fa, fb = _family(a), _family(b)
    if fa != fb:
        return False
    if fa == "list":
        return len(a) == len(b) and all(
            _values_equal(x, y) for x, y in zip(a, b)
        )
    return a == b


def _order_key(v: Any, *, op: str, side: str):
    if not isinstance(v, (str, Real)) or isinstance(v, bool):
        raise EvaluationError(
            f"operator {op!r} requires strings or numbers on each side; "
            f"{side} is {_family(v)}"
        )
    return v


def _is_member(item: Any, collection: list) -> bool:
    return any(_values_equal(item, member) for member in collection)


# ---------------------------------------------------------------------------
# Value evaluation (returns concrete JSON values; missing -> _MISSING)
# ---------------------------------------------------------------------------

def eval_value(node: dict, subject: AttrBag, resource: AttrBag, *,
               path: str = "$") -> Any:
    validate_node(node, path=path)
    op = node["op"]

    if op == "lit":
        return node["value"]

    if op == "attr":
        bag = subject if node["bag"] == "subject" else resource
        return bag.get(node["key"])

    if op == "exists":
        bag = subject if node["bag"] == "subject" else resource
        return bag.contains_key(node["key"])

    if op in _LOGICAL_OPS:
        return _eval_logical_value(op, node["args"], subject, resource, path)

    return _eval_binary_value(op, node, subject, resource, path)


def _eval_logical_value(op, args, subject, resource, path) -> Any:
    vals = [
        eval_value(a, subject, resource, path=f"{path}.args[{i}]")
        for i, a in enumerate(args)
    ]
    # Logical operators coerce values to the Kleene lattice: a missing
    # operand is UNKNOWN, a bool is itself, anything else is a hard error
    # (numbers are not truthy here — implicit truthiness is a footgun).
    tris = [_coerce_boolish(v, op, f"{path}.args[{i}]")
            for i, v in enumerate(vals)]
    if op == "not":
        return tri_not(tris[0])
    acc = tris[0]
    rest = tris[1:]
    reducer = tri_and if op == "and" else tri_or
    for t in rest:
        acc = reducer(acc, t)
    return acc


def _coerce_boolish(v: Any, op: str, path: str) -> Tri:
    if v is _MISSING:
        return Tri.UNKNOWN
    if isinstance(v, Tri):
        return v
    if isinstance(v, bool):
        return Tri.TRUE if v else Tri.FALSE
    raise EvaluationError(
        f"operator {op!r} requires boolean operands; got {_family(v)}",
        path=path,
    )


def _eval_binary_value(op, node, subject, resource, path) -> Any:
    left = eval_value(node["left"], subject, resource, path=f"{path}.left")
    right = eval_value(node["right"], subject, resource, path=f"{path}.right")
    missing_left = left is _MISSING
    missing_right = right is _MISSING

    # exists() never returns _MISSING, so a missing operand here always
    # means an absent attribute (or a set containing one).
    if op in ("eq", "ne"):
        if missing_left or missing_right:
            return Tri.UNKNOWN
        same = _values_equal(left, right)
        return Tri.TRUE if (same if op == "eq" else not same) else Tri.FALSE

    if op in ("lt", "lte", "gt", "gte"):
        if missing_left or missing_right:
            return Tri.UNKNOWN
        lk = _order_key(left, op=op, side="left")
        rk = _order_key(right, op=op, side="right")
        if _family(lk) != _family(rk):
            raise EvaluationError(
                f"operator {op!r} cannot compare {_family(lk)} and "
                f"{_family(rk)}", path=path
            )
        if op == "lt":
            ok = lk < rk
        elif op == "lte":
            ok = lk <= rk
        elif op == "gt":
            ok = lk > rk
        else:
            ok = lk >= rk
        return Tri.TRUE if ok else Tri.FALSE

    if op == "in":
        if missing_right:
            return Tri.UNKNOWN
        if missing_left:
            return Tri.UNKNOWN
        if not isinstance(right, list):
            raise EvaluationError(
                "'in' requires a list on the right", path=f"{path}.right"
            )
        return Tri.TRUE if _is_member(left, right) else Tri.FALSE

    if op == "contains":
        if missing_left:
            return Tri.UNKNOWN
        if missing_right:
            return Tri.UNKNOWN
        if not isinstance(left, list):
            raise EvaluationError(
                "'contains' requires a list on the left", path=f"{path}.left"
            )
        return Tri.TRUE if _is_member(right, left) else Tri.FALSE

    # subset: both sides must be lists; any absent side -> UNKNOWN.
    if op == "subset":
        if missing_left or missing_right:
            return Tri.UNKNOWN
        if not isinstance(left, list):
            raise EvaluationError(
                "'subset' requires lists on both sides", path=f"{path}.left"
            )
        if not isinstance(right, list):
            raise EvaluationError(
                "'subset' requires lists on both sides", path=f"{path}.right"
            )
        return Tri.TRUE if all(_is_member(x, right) for x in left) else Tri.FALSE

    raise EvaluationError(f"unsupported operator {op!r}", path=path)  # pragma: no cover


def eval_condition(node: dict, subject: AttrBag, resource: AttrBag, *,
                   path: str = "$") -> Tri:
    """Evaluate a condition node and normalise the result to a Tri value."""
    v = eval_value(node, subject, resource, path=path)
    if v is _MISSING:
        return Tri.UNKNOWN
    if isinstance(v, Tri):
        return v
    if isinstance(v, bool):
        return Tri.TRUE if v else Tri.FALSE
    raise EvaluationError(
        f"rule condition must decide to true/false/unknown; "
        f"it produced {_family(v)}",
        path=path,
    )


# ---------------------------------------------------------------------------
# Static validation (also invoked on every evaluation; cheap and defensive)
# ---------------------------------------------------------------------------

def validate_node(node: Any, *, path: str = "$") -> None:
    if not isinstance(node, dict):
        raise PolicyValidationError("expression node must be an object", path=path)
    if "op" not in node:
        raise PolicyValidationError("expression node is missing 'op'", path=path)
    op = node["op"]
    if not isinstance(op, str) or op not in ALL_OPS:
        raise PolicyValidationError(
            f"unknown operator {op!r}; allowed: {sorted(ALL_OPS)}", path=path
        )

    if op == "lit":
        if "value" not in node:
            raise PolicyValidationError("'lit' requires 'value'", path=path)
        _reject_extra(node, {"op", "value"}, path)
        if not is_literal(node["value"]):
            raise PolicyValidationError(
                "'lit' value must be a JSON scalar or homogeneous scalar list",
                path=f"{path}.value",
            )
        return

    if op in ("attr", "exists"):
        _require_key(node, "bag", path, str, BAGS)
        _require_key(node, "key", path, str)
        _reject_extra(node, {"op", "bag", "key"}, path)
        return

    if op in _LOGICAL_OPS:
        args = node.get("args")
        if not isinstance(args, list) or not args:
            raise PolicyValidationError(
                f"{op!r} requires a non-empty 'args' list", path=path
            )
        if op == "not" and len(args) != 1:
            raise PolicyValidationError(
                "'not' takes exactly one argument", path=path
            )
        _reject_extra(node, {"op", "args"}, path)
        for i, child in enumerate(args):
            validate_node(child, path=f"{path}.args[{i}]")
        return

    # binary ops
    if "left" not in node:
        raise PolicyValidationError(f"{op!r} requires 'left'", path=path)
    if "right" not in node:
        raise PolicyValidationError(f"{op!r} requires 'right'", path=path)
    _reject_extra(node, {"op", "left", "right"}, path)
    validate_node(node["left"], path=f"{path}.left")
    validate_node(node["right"], path=f"{path}.right")


def _require_key(node, key, path, typ, allowed=None):
    v = node.get(key)
    if not isinstance(v, typ) or (typ is str and v == ""):
        raise PolicyValidationError(
            f"{key!r} must be a non-empty {typ.__name__}", path=path
        )
    if allowed is not None and v not in allowed:
        raise PolicyValidationError(
            f"{key!r} must be one of {list(allowed)}", path=path
        )


def _reject_extra(node, allowed, path):
    extra = set(node) - allowed
    if extra:
        raise PolicyValidationError(
            f"unexpected field(s) {sorted(extra)}", path=path
        )
