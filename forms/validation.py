"""Validate submitted record data against a *specific* published version.

Every historical submission is validated by the version the device pinned,
never by the newest version, so tightening a rule in v3 cannot retroactively
invalidate data collected against v2.
"""
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime


@dataclass
class ValidationOutcome:
    ok: bool
    errors: dict = dc_field(default_factory=dict)  # field_key -> message

    def raise_if_invalid(self):
        if not self.ok:
            raise RecordValidationError(self.errors)


class RecordValidationError(Exception):
    """Each message is keyed by field; ``__all__`` carries form-level errors."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))


def _is_missing(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def _rule_matches(rule, data):
    ref_value = data.get(rule["field"])
    op = rule["op"]
    expected = rule.get("value")
    if op == "not_blank":
        return not _is_missing(ref_value)
    if op == "==":
        return ref_value == expected
    if op == "!=":
        return ref_value != expected
    if op == "in":
        return ref_value in expected
    if op == "not_in":
        return ref_value not in expected
    return False


def _coerce_value(ftype, value):
    """Best-effort coercion for JSON transport; return (value, error)."""
    if ftype == "number":
        if isinstance(value, bool):
            return None, "must be a number"
        if isinstance(value, (int, float)):
            return value, None
        try:
            return float(value), None
        except (TypeError, ValueError):
            return None, "must be a number"
    if ftype == "date":
        if isinstance(value, datetime):
            return value.date().isoformat(), None
        if isinstance(value, date):
            return value.isoformat(), None
        text = str(value).strip()
        try:
            return date.fromisoformat(text).isoformat(), None
        except ValueError:
            return None, "must be an ISO-8601 date (YYYY-MM-DD)"
    return value, None


def validate_record_data(template_version, data):
    """Validate one ``data`` object against an immutable template version.

    Unknown keys are rejected (a client on an old schema must keep its extra
    local fields local rather than silently dropping them server-side).
    """
    errors = {}
    if not isinstance(data, dict):
        return ValidationOutcome(False, {"__all__": "data must be an object"}), {}

    schema = template_version.field_map

    for key in data:
        if key not in schema:
            errors[key] = "unknown field for this template version"
    if errors:
        return ValidationOutcome(False, errors), {}

    normalized = {}
    for key, spec in schema.items():
        ftype = spec["type"]
        value = data.get(key)
        missing = _is_missing(value)

        required = bool(spec.get("required"))
        if not required and spec.get("required_if"):
            required = _rule_matches(spec["required_if"], data)

        if missing:
            if required:
                errors[key] = "this field is required"
            else:
                normalized[key] = None
            continue

        coerced, error = _coerce_value(ftype, value)
        if error:
            errors[key] = error
            continue

        if ftype == "text":
            text = str(coerced)
            if len(text) > 10000:
                errors[key] = "text is longer than 10000 characters"
                continue
            normalized[key] = text
        elif ftype == "number":
            normalized[key] = coerced
        elif ftype == "enum":
            if coerced not in spec["options"]:
                errors[key] = (
                    f"must be one of: {', '.join(map(str, spec['options']))}"
                )
                continue
            normalized[key] = coerced
        elif ftype == "date":
            normalized[key] = coerced

    return ValidationOutcome(not errors, errors), normalized


def check_version_accepted(template, template_version):
    """Compatibility policy for an old device pinning a previous version.

    The *newest* published snapshot declares the floor (its
    ``min_supported_version``). A submission is accepted as long as its pinned
    version is at least that floor; otherwise the device must upgrade. This
    lets an admin deliberately retire a schema after a field was deleted or a
    rule was tightened, without ever deleting or rewriting historical rows.
    """
    newest = (
        template.versions.order_by("-version").only("version", "min_supported_version").first()
    )
    floor = newest.min_supported_version if newest else 1
    if template_version.version < floor:
        raise RecordValidationError(
            {
                "__all__": (
                    f"template v{template_version.version} is below the minimum "
                    f"supported version v{floor}; upgrade the client template"
                )
            }
        )
