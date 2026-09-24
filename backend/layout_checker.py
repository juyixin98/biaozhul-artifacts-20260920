"""Storage-layout compatibility checker for upgradeable contracts.

Compares two Solidity compiler storage-layout JSON objects (the
``storageLayout`` output: ``{"storage": [...], "types": {...}}``) and decides
whether the *new* layout can safely replace the *old* one behind a proxy.

Rules enforced
--------------
error   VARIABLE_REMOVED    an old variable no longer exists in the new layout
error   SLOT_MOVED          an old variable sits in a different slot
error   OFFSET_MOVED        an old variable sits at a different byte offset
error   TYPE_CHANGED        an old variable's type changed (encoding, width,
                            struct members, array base/length, mapping
                            key/value — checked recursively)
error   VARIABLE_INSERTED   a new variable claims a slot/offset at or below
                            the old layout's last slot, shifting existing data
warning DUPLICATE_LABEL     a label appears more than once (matching is
                            first-found; common with private base variables)
warning INHERITANCE_CHANGED the ordered list of ancestor contracts differs
                            between versions (requires the caller to pass
                            ``old_bases`` / ``new_bases``, e.g. from the
                            artifact AST — solc's layout JSON always names the
                            most-derived contract, so bases cannot be read
                            from the layout itself)
info    VARIABLE_APPENDED   a new variable was appended after the old layout
                            (this is the only allowed kind of addition)

Dynamic types (``mapping``, ``dynamic_array``, ``bytes``/``string``) are
compared by encoding and, recursively, by their key/value/base types, so
e.g. ``uint256[]`` -> ``mapping(...)`` or ``string`` -> ``bytes`` is caught.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ERROR = "error"
WARNING = "warning"
INFO = "info"

DYNAMIC_ENCODINGS = {"mapping", "dynamic_array", "bytes"}

_MAX_DEPTH = 32


@dataclass
class Issue:
    severity: str
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code, "message": self.message}


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)

    @property
    def compatible(self) -> bool:
        return not any(i.severity == ERROR for i in self.issues)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == WARNING]

    def add(self, severity: str, code: str, message: str) -> None:
        self.issues.append(Issue(severity, code, message))

    def to_dict(self) -> dict:
        return {
            "compatible": self.compatible,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [i.to_dict() for i in self.issues],
        }


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _strip_roots(label: str, roots: tuple[str | None, str | None]) -> str:
    """Remove the two root contract names from qualified type labels.

    ``struct BoxV1.Config`` and ``struct BoxV2.Config`` describe the same
    type across a legitimate upgrade, so the root names are normalised away
    before labels are compared.
    """
    for root in roots:
        if root:
            label = label.replace(f"{root}.", "").replace(f"{root} ", "")
    return label


def _type_diffs(
    old_id: str | None,
    new_id: str | None,
    old_types: dict,
    new_types: dict,
    path: str,
    roots: tuple[str | None, str | None],
    depth: int = 0,
) -> list[str]:
    """Recursively compare two type ids; return human-readable differences."""
    if old_id == new_id and old_types is new_types:
        return []
    if depth > _MAX_DEPTH:
        return [f"{path}: type recursion limit exceeded"]
    if old_id is None or new_id is None:
        if old_id != new_id:
            return [f"{path}: type structure changed ({old_id} -> {new_id})"]
        return []

    old = old_types.get(old_id)
    new = new_types.get(new_id)
    if old is None or new is None:
        # Type ids embed the AST, so identical types in different compilations
        # share ids; differing ids with missing definitions are suspicious.
        if old_id != new_id:
            return [f"{path}: type id changed ({old_id} -> {new_id}), definition unavailable"]
        return []

    diffs: list[str] = []

    old_enc, new_enc = old.get("encoding"), new.get("encoding")
    if old_enc != new_enc:
        diffs.append(f"{path}: encoding changed {old_enc!r} -> {new_enc!r}")

    old_label = _strip_roots(str(old.get("label", "")), roots)
    new_label = _strip_roots(str(new.get("label", "")), roots)
    if old_label != new_label:
        diffs.append(f"{path}: type label changed {old_label!r} -> {new_label!r}")

    if _int(old.get("numberOfBytes")) != _int(new.get("numberOfBytes")):
        diffs.append(
            f"{path}: size changed {old.get('numberOfBytes')} -> {new.get('numberOfBytes')} bytes"
        )

    # Structs: compare members one by one (labels, relative slots/offsets, types).
    old_members = old.get("members")
    new_members = new.get("members")
    if old_members is not None or new_members is not None:
        old_members = old_members or []
        new_members = new_members or []
        if len(old_members) != len(new_members):
            diffs.append(
                f"{path}: struct member count changed {len(old_members)} -> {len(new_members)}"
            )
        for om, nm in zip(old_members, new_members):
            mpath = f"{path}.{om.get('label', '?')}"
            if om.get("label") != nm.get("label"):
                diffs.append(f"{mpath}: member renamed to {nm.get('label')!r}")
            if _int(om.get("slot")) != _int(nm.get("slot")):
                diffs.append(f"{mpath}: member slot moved {om.get('slot')} -> {nm.get('slot')}")
            if _int(om.get("offset")) != _int(nm.get("offset")):
                diffs.append(
                    f"{mpath}: member offset moved {om.get('offset')} -> {nm.get('offset')}"
                )
            diffs += _type_diffs(
                om.get("type"), nm.get("type"), old_types, new_types, mpath, roots, depth + 1
            )

    # Arrays (static and dynamic): base type and, for static arrays, length.
    if old.get("base") is not None or new.get("base") is not None:
        diffs += _type_diffs(
            old.get("base"), new.get("base"), old_types, new_types, f"{path}[]", roots, depth + 1
        )
        if old.get("length") != new.get("length"):
            diffs.append(f"{path}: array length changed {old.get('length')} -> {new.get('length')}")

    # Mappings: key and value types.
    for part in ("key", "value"):
        if old.get(part) is not None or new.get(part) is not None:
            diffs += _type_diffs(
                old.get(part),
                new.get(part),
                old_types,
                new_types,
                f"{path}<{part}>",
                roots,
                depth + 1,
            )

    return diffs


def check_layouts(
    old_layout: dict,
    new_layout: dict,
    old_name: str | None = None,
    new_name: str | None = None,
    old_bases: list[str] | None = None,
    new_bases: list[str] | None = None,
) -> Report:
    """Compare two compiler storage layouts.

    ``old_name`` / ``new_name`` are the root (most-derived) contract names;
    they are used to normalise type labels. ``old_bases`` / ``new_bases``
    are the ancestor contract names in linearized order (from the artifact
    AST); when both are given, a difference is reported as
    INHERITANCE_CHANGED.
    """
    report = Report()
    roots = (old_name, new_name)

    old_storage = old_layout.get("storage") or []
    new_storage = new_layout.get("storage") or []
    old_types = old_layout.get("types") or {}
    new_types = new_layout.get("types") or {}

    # Index new variables by label.
    new_by_label: dict[str, list[dict]] = {}
    for var in new_storage:
        new_by_label.setdefault(var["label"], []).append(var)
    for source, storage in (("old", old_storage), ("new", new_storage)):
        seen: dict[str, int] = {}
        for var in storage:
            seen[var["label"]] = seen.get(var["label"], 0) + 1
        for label, count in seen.items():
            if count > 1:
                report.add(
                    WARNING,
                    "DUPLICATE_LABEL",
                    f"{source} layout: label {label!r} appears {count} times "
                    "(matched by first occurrence)",
                )

    # Old layout footprint: highest slot used and offsets used per slot.
    max_old_slot = -1
    old_offsets_by_slot: dict[int, set[int]] = {}
    for var in old_storage:
        slot = _int(var.get("slot"))
        offset = _int(var.get("offset"))
        max_old_slot = max(max_old_slot, slot)
        old_offsets_by_slot.setdefault(slot, set()).add(offset)

    # Every old variable must survive unchanged.
    matched_new_ids: set[int] = set()
    for var in old_storage:
        label = var["label"]
        candidates = new_by_label.get(label, [])
        if not candidates:
            report.add(ERROR, "VARIABLE_REMOVED", f"variable {label!r} was removed")
            continue
        new_var = candidates[0]
        matched_new_ids.add(id(new_var))

        old_slot, new_slot = _int(var.get("slot")), _int(new_var.get("slot"))
        if old_slot != new_slot:
            report.add(
                ERROR,
                "SLOT_MOVED",
                f"variable {label!r} moved from slot {old_slot} to slot {new_slot}",
            )
        old_off, new_off = _int(var.get("offset")), _int(new_var.get("offset"))
        if old_off != new_off:
            report.add(
                ERROR,
                "OFFSET_MOVED",
                f"variable {label!r} moved from byte offset {old_off} to {new_off} "
                f"in slot {old_slot}",
            )
        for diff in _type_diffs(
            var.get("type"), new_var.get("type"), old_types, new_types, label, roots
        ):
            report.add(ERROR, "TYPE_CHANGED", diff)

    # New variables may only be appended beyond the old footprint.
    for var in new_storage:
        if id(var) in matched_new_ids:
            continue
        label = var["label"]
        slot = _int(var.get("slot"))
        offset = _int(var.get("offset"))
        collides = slot < max_old_slot or (
            slot == max_old_slot and offset in old_offsets_by_slot.get(slot, set())
        )
        if collides:
            report.add(
                ERROR,
                "VARIABLE_INSERTED",
                f"new variable {label!r} inserted at slot {slot} offset {offset}, "
                f"inside the old layout's footprint (last old slot: {max_old_slot})",
            )
        else:
            enc = (new_types.get(var.get("type")) or {}).get("encoding", "inplace")
            kind = "dynamic " if enc in DYNAMIC_ENCODINGS else ""
            report.add(
                INFO,
                "VARIABLE_APPENDED",
                f"new {kind}variable {label!r} appended at slot {slot} offset {offset}",
            )

    # Inheritance diff (diagnostic; slot shifts above are what actually block).
    if old_bases is not None and new_bases is not None and old_bases != new_bases:
        added = [b for b in new_bases if b not in old_bases]
        removed = [b for b in old_bases if b not in new_bases]
        detail = []
        if added:
            detail.append(f"added: {', '.join(added)}")
        if removed:
            detail.append(f"removed: {', '.join(removed)}")
        if not detail:
            detail.append("order changed")
        report.add(
            WARNING,
            "INHERITANCE_CHANGED",
            "base contracts changed (" + "; ".join(detail) + ")",
        )

    return report
