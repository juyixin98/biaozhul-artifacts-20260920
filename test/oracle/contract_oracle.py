#!/usr/bin/env python3
"""
Independent reference oracle for the OpenAPI 3.1 subset contract diff service.

This file is intentionally pure-stdlib Python and shares NO code with the
TypeScript implementation under test. It independently derives:

  1. the expected set of findings  (direction, severity, code, operation, path)
  2. an independent accept/reject decision for every concrete witness value,
     checked against the OLD and NEW raw contracts themselves.

The TypeScript test-suite calls this oracle through a tiny JSON stdio bridge
(`oracle_bridge.py`) so that expected answers never come from the system under
test.

Rules implemented (subset):
  request direction : producer = OLD client values, consumer = NEW server
  response direction: producer = NEW server values, consumer = OLD client
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Optional

HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
TYPE_ACCEPT = {
    ("integer", "number"),
}
# root/field code tables, mirroring the vocabulary in src/core/types.ts
PARAM_ROOT = dict(
    narrow="PARAM_TYPE_NARROWED",
    enum_narrow="PARAM_ENUM_NARROWED",
    enum_relaxed="PARAM_ENUM_RELAXED",
    enum_widen="PARAM_ENUM_EXTENDED",
    default_change="PARAM_DEFAULT_CHANGED",
    default_remove="PARAM_DEFAULT_REMOVED",
    default_add="PARAM_DEFAULT_ADDED",
)
BODY_ROOT = dict(
    narrow="REQUEST_BODY_TYPE_NARROWED",
    enum_narrow="REQUEST_BODY_ENUM_NARROWED",
    enum_relaxed="REQUEST_BODY_ENUM_RELAXED",
    enum_widen="REQUEST_BODY_ENUM_EXTENDED",
    default_change="REQUEST_BODY_DEFAULT_CHANGED",
    default_remove="REQUEST_BODY_DEFAULT_REMOVED",
    default_add="REQUEST_BODY_DEFAULT_ADDED",
)
BODY_FIELD = dict(
    narrow="REQUEST_BODY_FIELD_TYPE_NARROWED",
    enum_narrow="REQUEST_BODY_FIELD_ENUM_NARROWED",
    enum_relaxed="REQUEST_BODY_FIELD_ENUM_RELAXED",
    enum_widen="REQUEST_BODY_FIELD_ENUM_EXTENDED",
    default_change="REQUEST_BODY_FIELD_DEFAULT_CHANGED",
    default_remove="REQUEST_BODY_FIELD_DEFAULT_REMOVED",
    default_add="REQUEST_BODY_FIELD_DEFAULT_ADDED",
)
RESP_ROOT = dict(
    narrow="RESPONSE_TYPE_NARROWED",
    enum_narrow="RESPONSE_ENUM_NARROWED",
    enum_relaxed="RESPONSE_ENUM_RELAXED",
    enum_widen="RESPONSE_ENUM_EXTENDED",
    default_change="RESPONSE_FIELD_DEFAULT_CHANGED",
    default_remove="RESPONSE_FIELD_DEFAULT_CHANGED",
    default_add="RESPONSE_FIELD_DEFAULT_CHANGED",
)
RESP_FIELD = dict(
    narrow="RESPONSE_FIELD_TYPE_NARROWED",
    enum_narrow="RESPONSE_FIELD_ENUM_NARROWED",
    enum_relaxed="RESPONSE_FIELD_ENUM_RELAXED",
    enum_widen="RESPONSE_FIELD_ENUM_EXTENDED",
    default_change="RESPONSE_FIELD_DEFAULT_CHANGED",
    default_remove="RESPONSE_FIELD_DEFAULT_CHANGED",
    default_add="RESPONSE_FIELD_DEFAULT_CHANGED",
)


@dataclass(frozen=True)
class ExpectedFinding:
    direction: str
    severity: str
    code: str
    operation: str
    path: str

    def key(self) -> list[Any]:
        return [self.direction, self.severity, self.code, self.operation, self.path]


# ---------------------------------------------------------------------------
# raw document helpers + bounded $ref handling
# ---------------------------------------------------------------------------

def _resolve_ref(root: dict, ref: str) -> Optional[Any]:
    if not ref.startswith("#/"):
        return None
    cur: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def deref(root: dict, node: Any, max_hops: int = 32) -> tuple[Any, Optional[str]]:
    """Follow local $refs; return (node, problem)."""
    seen: list[str] = []
    for _ in range(max_hops + 1):
        if not isinstance(node, dict) or "$ref" not in node:
            return node, None
        ref = node["$ref"]
        if ref in seen:
            return node, "REF_CYCLE"
        seen.append(ref)
        target = _resolve_ref(root, ref)
        if target is None:
            return node, "UNRESOLVED_REF"
        node = target
    return node, "REF_DEPTH_LIMIT"


def _types(schema: dict) -> list[str]:
    t = schema.get("type", [])
    if isinstance(t, str):
        return [t]
    if isinstance(t, list):
        return [x for x in t if isinstance(x, str)]
    return []


def _json_equal(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def join_path(base: str, name: str) -> str:
    return f"$.{name}" if not base else f"{base}.{name}"


# ---------------------------------------------------------------------------
# finding accumulation
# ---------------------------------------------------------------------------

class Findings:
    def __init__(self) -> None:
        self.items: list[ExpectedFinding] = []

    def add(self, direction: str, severity: str, code: str, op: str, path: str = "") -> None:
        self.items.append(ExpectedFinding(direction, severity, code, op, path))


# ---------------------------------------------------------------------------
# schema comparison (directional, shared for request/response)
# ---------------------------------------------------------------------------

def type_rejected(producer: dict, consumer: dict) -> Optional[str]:
    pt, ct = _types(producer), _types(consumer)
    if not pt or not ct:
        return None
    for t in pt:
        if t in ct:
            continue
        if t == "integer" and "number" in ct:
            continue
        return t
    return None


def enum_escape(producer: dict, consumer: dict) -> Optional[Any]:
    """A producer-allowed value the consumer rejects on type/enum surface."""
    c_enum = consumer.get("enum")
    if not isinstance(c_enum, list):
        return None
    p_enum = producer.get("enum")
    if isinstance(p_enum, list):
        for lit in p_enum:
            if not _value_accepted(lit, consumer):
                return lit
        return None
    # producer unrestricted beyond type: synthesise a value
    taken = {v for v in c_enum if isinstance(v, str)}
    for cand in ("other", "unknown", "x-other", "zzz"):
        if cand not in taken and not _value_accepted(cand, consumer):
            return cand
    return None


def _matches_type(value: Any, t: str) -> bool:
    if t == "null":
        return value is None
    if t == "boolean":
        return isinstance(value, bool)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    return True


def _value_accepted(value: Any, schema: dict) -> bool:
    types = _types(schema)
    if types and not any(_matches_type(value, t) for t in types):
        return False
    enum = schema.get("enum")
    if isinstance(enum, list) and not any(_json_equal(value, e) for e in enum):
        return False
    return True


def compare_schema(
    root_p: dict, root_c: dict,
    producer: dict, consumer: dict,
    direction: str, op: str,
    codes_root: dict, codes_field: dict,
    findings: Findings, path: str = "",
) -> None:
    codes = codes_root if path == "" else codes_field

    rejected = type_rejected(producer, consumer)
    if rejected is not None:
        findings.add(direction, "BREAKING", codes["narrow"], op, path)
        return

    p_enum = producer.get("enum")
    c_enum = consumer.get("enum")
    if not isinstance(p_enum, list) and isinstance(c_enum, list):
        if enum_escape(producer, consumer) is not None:
            findings.add(direction, "BREAKING", codes["enum_narrow"], op, path)
    elif isinstance(p_enum, list) and not isinstance(c_enum, list):
        findings.add(direction, "NON_BREAKING", codes["enum_relaxed"], op, path)
    elif isinstance(p_enum, list) and isinstance(c_enum, list):
        if enum_escape(producer, consumer) is not None:
            findings.add(direction, "BREAKING", codes["enum_narrow"], op, path)
        elif any(not any(_json_equal(v, e) for e in p_enum) for v in c_enum):
            findings.add(direction, "NON_BREAKING", codes["enum_widen"], op, path)

    if "default" in producer and "default" in consumer and not _json_equal(
        producer["default"], consumer["default"]
    ):
        findings.add(direction, "NON_BREAKING", codes["default_change"], op, path)
    elif "default" in producer and "default" not in consumer:
        findings.add(direction, "NON_BREAKING", codes["default_remove"], op, path)
    elif "default" not in producer and "default" in consumer:
        findings.add(direction, "NON_BREAKING", codes["default_add"], op, path)

    if _allows_object(producer) and _allows_object(consumer):
        p_props = producer.get("properties", {}) or {}
        c_props = consumer.get("properties", {}) or {}
        p_req = set(producer.get("required", []) or [])
        c_req = set(consumer.get("required", []) or [])

        for name, c_prop in c_props.items():
            if name in p_props:
                continue
            sub = join_path(path, name)
            if direction == "request":
                code = ("REQUEST_BODY_REQUIRED_FIELD_ADDED" if name in c_req
                        else "REQUEST_BODY_OPTIONAL_FIELD_ADDED")
                severity = "BREAKING" if name in c_req else "NON_BREAKING"
            else:
                code = ("RESPONSE_REQUIRED_FIELD_REMOVED" if name in c_req
                        else "RESPONSE_OPTIONAL_FIELD_REMOVED")
                severity = "BREAKING" if name in c_req else "NON_BREAKING"
            findings.add(direction, severity, code, op, sub)

        for name in p_props:
            if name not in c_props:
                sub = join_path(path, name)
                if direction == "request":
                    findings.add(direction, "NON_BREAKING", "REQUEST_BODY_FIELD_REMOVED", op, sub)
                else:
                    findings.add(direction, "NON_BREAKING", "RESPONSE_FIELD_ADDED", op, sub)

        for name, p_prop in p_props.items():
            if name not in c_props:
                continue
            c_prop = c_props[name]
            sub = join_path(path, name)
            p_r, c_r = name in p_req, name in c_req
            if p_r != c_r:
                if direction == "request":
                    if c_r:
                        findings.add(direction, "BREAKING",
                                     "REQUEST_BODY_FIELD_BECAME_REQUIRED", op, sub)
                    else:
                        findings.add(direction, "NON_BREAKING",
                                     "REQUEST_BODY_FIELD_BECAME_OPTIONAL", op, sub)
                else:
                    if c_r:
                        # old consumer requires, new producer only sends
                        # optionally -> server may omit it: BREAKING
                        findings.add(direction, "BREAKING",
                                     "RESPONSE_FIELD_BECAME_OPTIONAL", op, sub)
                    else:
                        findings.add(direction, "NON_BREAKING",
                                     "RESPONSE_FIELD_BECAME_REQUIRED", op, sub)
            compare_schema(root_p, root_c, p_prop, c_prop, direction, op,
                           codes_root, codes_field, findings, sub)

    if isinstance(producer.get("items"), dict) and isinstance(consumer.get("items"), dict):
        compare_schema(root_p, root_c, producer["items"], consumer["items"],
                       direction, op, codes_root, codes_field, findings,
                       f"{path}[]" if path else "$[]")


def _allows_object(schema: dict) -> bool:
    return not _types(schema) or "object" in _types(schema)


# ---------------------------------------------------------------------------
# operation / parameter / body / response rules
# ---------------------------------------------------------------------------

def _params(root: dict, op: dict, path_item: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for container in (path_item.get("parameters", []), op.get("parameters", [])):
        if not isinstance(container, list):
            continue
        for p in container:
            p, _ = deref(root, p)
            if not isinstance(p, dict):
                continue
            name, loc = p.get("name"), p.get("in")
            if isinstance(name, str) and loc in ("query", "header", "path", "cookie"):
                required = p.get("required") is True or loc == "path"
                out[f"{loc}:{name}"] = {**p, "required": required}
    return out


def _json_schema(content: Optional[dict]) -> Optional[dict]:
    if not isinstance(content, dict):
        return None
    for mt, media in content.items():
        if mt == "application/json" or mt.endswith("+json"):
            schema = media.get("schema") if isinstance(media, dict) else None
            return schema if isinstance(schema, dict) else None
    return None


def expected_findings(old_doc: dict, new_doc: dict) -> list[ExpectedFinding]:
    findings = Findings()
    old_paths = old_doc.get("paths", {}) or {}
    new_paths = new_doc.get("paths", {}) or {}

    old_ops = _operations(old_doc, old_paths)
    new_ops = _operations(new_doc, new_paths)

    for key in sorted(set(old_ops) | set(new_ops)):
        if key in old_ops and key not in new_ops:
            findings.add("request", "BREAKING", "OPERATION_REMOVED", key)
            continue
        if key not in old_ops and key in new_ops:
            findings.add("request", "NON_BREAKING", "OPERATION_ADDED", key)
            continue

        old_method, old_path, old_op, old_item = old_ops[key]
        _, _, new_op, new_item = new_ops[key]
        old_params = _params(old_doc, old_op, old_item)
        new_params = _params(new_doc, new_op, new_item)

        # same name, different location (only when the new key itself is
        # absent old-side; same-name params coexisting at two locations stay
        # distinct additions)
        old_by_name = {p["name"]: p for p in old_params.values()}
        relocated: set[str] = set()
        for nk, np in new_params.items():
            if nk in old_params:
                continue
            op_p = old_by_name.get(np["name"])
            # pure move only when the old same-name key also disappears
            if op_p is not None and f'{op_p["in"]}:{op_p["name"]}' not in new_params:
                findings.add("request", "BREAKING", "PARAM_LOCATION_CHANGED", key)
                relocated.add(f'{op_p["in"]}:{op_p["name"]}')
                relocated.add(nk)

        for nk, np in new_params.items():
            if nk in relocated:
                continue
            op_p = old_params.get(nk)
            if op_p is None:
                if np["required"]:
                    findings.add("request", "BREAKING", "REQUIRED_PARAM_ADDED", key)
                else:
                    findings.add("request", "NON_BREAKING", "OPTIONAL_PARAM_ADDED", key)
                continue
            if not op_p["required"] and np["required"]:
                findings.add("request", "BREAKING", "PARAM_BECAME_REQUIRED", key)
            elif op_p["required"] and not np["required"]:
                findings.add("request", "NON_BREAKING", "PARAM_BECAME_OPTIONAL", key)
            ps, cs = op_p.get("schema", {}), np.get("schema", {})
            if isinstance(ps, dict) and isinstance(cs, dict):
                ps2, _ = deref(old_doc, ps)
                cs2, _ = deref(new_doc, cs)
                compare_schema(old_doc, new_doc, ps2, cs2, "request", key,
                               PARAM_ROOT, PARAM_ROOT, findings)

        for ok, op_p in old_params.items():
            if ok in relocated:
                continue
            if ok not in new_params:
                findings.add("request", "NON_BREAKING", "PARAM_REMOVED", key)

        _compare_bodies(old_doc, new_doc, old_op, new_op, key, findings)
        _compare_responses(old_doc, new_doc, old_op, new_op, key, findings)

    return findings.items


def _operations(doc: dict, paths: dict) -> dict[str, tuple[str, str, dict, dict]]:
    out: dict[str, tuple[str, str, dict, dict]] = {}
    for path, raw_item in paths.items():
        if path.startswith("x-"):
            continue
        item, _ = deref(doc, raw_item)
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() in HTTP_METHODS and isinstance(op, dict):
                op2, _ = deref(doc, op)
                out[f"{method.upper()} {path}"] = (method.upper(), path, op2, item)
    return out


def _compare_bodies(old_doc, new_doc, old_op, new_op, key, findings) -> None:
    old_body = old_op.get("requestBody")
    new_body = new_op.get("requestBody")
    old_body, _ = deref(old_doc, old_body) if old_body else (None, None)
    new_body, _ = deref(new_doc, new_body) if new_body else (None, None)

    if old_body is None and isinstance(new_body, dict):
        if new_body.get("required") is True:
            findings.add("request", "BREAKING", "REQUEST_BODY_ADDED", key)
        else:
            findings.add("request", "NON_BREAKING", "REQUEST_BODY_ADDED", key)
        return
    if isinstance(old_body, dict) and new_body is None:
        findings.add("request", "NON_BREAKING", "REQUEST_BODY_REMOVED", key)
        return
    if not isinstance(old_body, dict) or not isinstance(new_body, dict):
        return
    if old_body.get("required") is not True and new_body.get("required") is True:
        findings.add("request", "BREAKING", "REQUEST_BODY_BECAME_REQUIRED", key)
    ps = _json_schema(old_body.get("content"))
    cs = _json_schema(new_body.get("content"))
    if isinstance(ps, dict) and isinstance(cs, dict):
        ps2, _ = deref(old_doc, ps)
        cs2, _ = deref(new_doc, cs)
        compare_schema(old_doc, new_doc, ps2, cs2, "request", key,
                       BODY_ROOT, BODY_FIELD, findings)


def _compare_responses(old_doc, new_doc, old_op, new_op, key, findings) -> None:
    old_resps = (old_op.get("responses") or {})
    new_resps = (new_op.get("responses") or {})
    for code in new_resps:
        if code == "default" or code.startswith("x-"):
            continue
        if code not in old_resps:
            findings.add("response", "BREAKING", "RESPONSE_STATUS_ADDED", key)
    for code in old_resps:
        if code == "default" or code.startswith("x-"):
            continue
        if code not in new_resps:
            findings.add("response", "NON_BREAKING", "RESPONSE_STATUS_REMOVED", key)
    for code, new_resp in new_resps.items():
        if code == "default" or code not in old_resps:
            continue
        old_resp, _ = deref(old_doc, old_resps[code])
        new_resp, _ = deref(new_doc, new_resp)
        if not isinstance(old_resp, dict) or not isinstance(new_resp, dict):
            continue
        ps = _json_schema(new_resp.get("content"))  # producer = NEW
        cs = _json_schema(old_resp.get("content"))  # consumer = OLD
        if isinstance(ps, dict) and isinstance(cs, dict):
            ps2, _ = deref(new_doc, ps)
            cs2, _ = deref(old_doc, cs)
            compare_schema(new_doc, old_doc, ps2, cs2, "response", key,
                           RESP_ROOT, RESP_FIELD, findings)


# ---------------------------------------------------------------------------
# independent witness validator
# ---------------------------------------------------------------------------

class SchemaViolation(Exception):
    pass


def validate_value(value: Any, schema: Any, root: dict, trail: Optional[list] = None) -> None:
    """Raise SchemaViolation when value is rejected by the subset schema."""
    trail = trail or []
    if not isinstance(schema, dict):
        return
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in trail:
            return  # bounded: treat cyclic ref as unconstrained
        target = _resolve_ref(root, ref)
        if target is None:
            raise SchemaViolation(f"unresolved $ref {ref}")
        validate_value(value, target, root, trail + [ref])
        return
    types = _types(schema)
    if types and not any(_matches_type(value, t) for t in types):
        raise SchemaViolation(f"type mismatch: {json.dumps(value)[:40]} not in {types}")
    enum = schema.get("enum")
    if isinstance(enum, list) and not any(_json_equal(value, e) for e in enum):
        raise SchemaViolation(f"enum violation: {json.dumps(value)[:40]}")
    if isinstance(value, dict) and (_allows_object(schema)):
        props = schema.get("properties", {}) or {}
        for req in schema.get("required", []) or []:
            if req not in value:
                raise SchemaViolation(f"missing required property {req}")
        for name, sub in props.items():
            if name in value:
                validate_value(value[name], sub, root, trail)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for item in value:
            validate_value(item, schema["items"], root, trail)


def accepted(value: Any, schema: Any, root: dict) -> bool:
    try:
        validate_value(value, schema, root)
        return True
    except SchemaViolation:
        return False


def _op_docs(doc: dict) -> dict[str, tuple[dict, dict, dict]]:
    """opKey -> (op, pathItem, doc)"""
    out = {}
    for path, raw_item in (doc.get("paths") or {}).items():
        item, _ = deref(doc, raw_item)
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() in HTTP_METHODS and isinstance(op, dict):
                op2, _ = deref(doc, op)
                out[f"{method.upper()} {path}"] = (op2, item, doc)
    return out


def verify_witness(finding: dict, old_doc: dict, new_doc: dict) -> dict:
    """
    Independently decide whether a BREAKING witness really is accepted on the
    producer side and rejected on the consumer side. Returns
    {ok, reason}.
    """
    code = finding["code"]
    direction = finding["direction"]
    op_key = finding["operation"]
    w = finding.get("witness") or {}
    example = w.get("example")

    old_map, new_map = _op_docs(old_doc), _op_docs(new_doc)

    if code == "OPERATION_REMOVED":
        return {"ok": op_key in old_map and op_key not in new_map,
                "reason": "operation must exist old-side and be absent new-side"}

    if op_key not in old_map or op_key not in new_map:
        return {"ok": False, "reason": "operation missing on one side"}
    old_op, old_item, _ = old_map[op_key]
    new_op, new_item, _ = new_map[op_key]
    old_params = _params(old_doc, old_op, old_item)
    new_params = _params(new_doc, new_op, new_item)

    if code == "PARAM_LOCATION_CHANGED":
        name, loc = example.get("name"), example.get("in")
        key = f"{loc}:{name}"
        old_ok = key in old_params and accepted(example.get("value"),
                                                old_params[key].get("schema", {}), old_doc)
        gone = key not in new_params
        return {"ok": old_ok and gone,
                "reason": f"old-side accepted={old_ok}, unreadable at new location={gone}"}

    if code in ("REQUIRED_PARAM_ADDED", "PARAM_BECAME_REQUIRED"):
        old_sat = _request_params_satisfied(example, old_params, old_doc)
        new_sat = _request_params_satisfied(example, new_params, new_doc)
        return {"ok": old_sat and not new_sat,
                "reason": f"old required params satisfied={old_sat}, new satisfied={new_sat}"}

    if code in ("PARAM_TYPE_NARROWED", "PARAM_ENUM_NARROWED"):
        name, loc, value = example.get("name"), example.get("in"), example.get("value")
        key = f"{loc}:{name}"
        old_ok = accepted(value, old_params[key].get("schema", {}), old_doc)
        new_ok = accepted(value, new_params[key].get("schema", {}), new_doc)
        return {"ok": old_ok and not new_ok,
                "reason": f"old-side accepted={old_ok}, new-side accepted={new_ok}"}

    if code in ("REQUEST_BODY_ADDED", "REQUEST_BODY_BECAME_REQUIRED"):
        new_required = _deref_body(new_doc, new_op).get("required") is True
        body = example.get("body") if isinstance(example, dict) else None
        old_body = _deref_body(old_doc, old_op)
        old_accept = True
        if old_body is not None and old_body.get("required") is True and body is None:
            old_accept = False
        return {"ok": new_required and body is None and old_accept,
                "reason": "bodyless request must be fine old-side but violate new required body"}

    if direction == "request" and code.startswith(("REQUEST_BODY_", "REQUEST_BODY_FIELD")):
        body = example.get("body")
        old_schema = _json_schema(_deref_body(old_doc, old_op).get("content") or {})
        new_schema = _json_schema(_deref_body(new_doc, new_op).get("content") or {})
        old_ok = old_schema is None or accepted(body, old_schema, old_doc)
        new_ok = new_schema is not None and accepted(body, new_schema, new_doc)
        return {"ok": old_ok and not new_ok,
                "reason": f"body old-side accepted={old_ok}, new-side accepted={new_ok}"}

    if code == "RESPONSE_STATUS_ADDED":
        status = str(example.get("status"))
        old_codes = {c for c in (old_op.get("responses") or {}) if c != "default"}
        absent_old = status not in old_codes
        new_resp, _ = deref(new_doc, (new_op.get("responses") or {}).get(status, {}))
        new_schema = _json_schema(new_resp.get("content") or {}) if isinstance(new_resp, dict) else None
        body = example.get("body")
        new_ok = new_schema is None or accepted(body, new_schema, new_doc)
        return {"ok": absent_old and new_ok,
                "reason": f"status {status} absent old-side={absent_old}, body new-valid={new_ok}"}

    if direction == "response":
        status = str(example.get("status"))
        body = example.get("body")
        new_resp, _ = deref(new_doc, (new_op.get("responses") or {}).get(status, {}))
        old_resp, _ = deref(old_doc, (old_op.get("responses") or {}).get(status, {}))
        new_schema = _json_schema(new_resp.get("content") or {})
        old_schema = _json_schema(old_resp.get("content") or {})
        new_ok = new_schema is None or accepted(body, new_schema, new_doc)
        old_ok = old_schema is not None and accepted(body, old_schema, old_doc)
        return {"ok": new_ok and not old_ok,
                "reason": f"body new-side accepted={new_ok}, old-side accepted={old_ok}"}

    return {"ok": False, "reason": f"no witness rule for {code}"}


def _deref_body(doc: dict, op: dict) -> Optional[dict]:
    body = op.get("requestBody")
    if body is None:
        return None
    body, _ = deref(doc, body)
    return body if isinstance(body, dict) else None


def _request_params_satisfied(example: dict, params: dict[str, dict], doc: dict) -> bool:
    if not isinstance(example, dict):
        return False
    q = example.get("query", {}) or {}
    h = {k.lower(): v for k, v in (example.get("headers", {}) or {}).items()}
    provided = set()
    for key, p in params.items():
        loc, name = key.split(":", 1)
        if loc == "query" and name in q:
            provided.add(key)
        elif loc == "header" and name.lower() in h:
            provided.add(key)
        elif loc == "path":
            provided.add(key)  # rendered path is assumed satisfied
        elif loc == "cookie":
            pass
        if key in provided:
            value = q.get(name) if loc == "query" else h.get(name.lower())
            if not accepted(value, p.get("schema", {}), doc):
                return False
    for key, p in params.items():
        if p["required"] and key not in provided:
            return False
    return True


# ---------------------------------------------------------------------------
# stdio JSON bridge:  {"old": {...}, "new": {...}} lines on stdin
# ---------------------------------------------------------------------------

def analyze(payload: dict) -> dict:
    old_doc, new_doc = payload["old"], payload["new"]
    expected = [f.key() for f in expected_findings(old_doc, new_doc)]
    verifications = []
    for f in payload.get("findings", []):
        if f.get("severity") != "BREAKING":
            continue
        verifications.append({"id": f.get("id"), **verify_witness(f, old_doc, new_doc)})
    uncertainties = (
        [dict(u, side="old") for u in collect_uncertainties(old_doc, old_doc)]
        + [dict(u, side="new") for u in collect_uncertainties(new_doc, new_doc)]
    )
    return {
        "expected": expected,
        "witnessChecks": verifications,
        "uncertainties": uncertainties,
    }


def collect_uncertainties(node: Any, root: dict, path: str = "$",
                          seen: Optional[list] = None) -> list[dict]:
    """Independently flag cycles, unresolved refs, unsupported keywords."""
    seen = seen if seen is not None else []
    out: list[dict] = []
    unsupported = {"oneOf", "anyOf", "allOf", "not"}
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            if ref in seen:
                out.append({"code": "REF_CYCLE", "location": path})
            else:
                target = _resolve_ref(root, ref)
                if target is None:
                    out.append({"code": "UNRESOLVED_REF", "location": path})
                elif len(seen) < 32:
                    out.extend(collect_uncertainties(target, root,
                                                      f"{path}->{ref}", seen + [ref]))
        for k, v in node.items():
            if k == "$ref":
                continue
            if k in unsupported and v is not None:
                out.append({"code": "UNSUPPORTED_KEYWORD", "location": f"{path}.{k}"})
            out.extend(collect_uncertainties(v, root, f"{path}.{k}", seen))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(collect_uncertainties(v, root, f"{path}[{i}]", seen))
    return out


def main() -> int:
    payload = json.load(sys.stdin)
    sys.stdout.write(json.dumps(analyze(payload)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
