"""Storage-layout compatibility checker for upgradeable Solidity contracts.

The checker consumes two compiler storage-layout JSON documents (the format
emitted by ``solc --storage-layout`` / ``forge build`` with
``extra_output = ["storageLayout"]``) and decides whether replacing the "old"
implementation with the "new" one is storage-safe.

Core rules (anything below is reported as an ``error`` and BLOCKS the
upgrade; informational findings use ``warning``):

1. Slot/offset preservation. Every variable present in both versions under the
   same label must keep its absolute slot and byte offset. Relocated variables
   corrupt old data.

2. Type preservation. The encoding-relevant type identity of a shared label
   may not change. Type identity is derived from:
     - encoding (inplace / dynamic_array / mapping / bytes),
     - number of bytes,
     - element type (for arrays),
     - key/value types (for mappings),
     - members (for structs).
   Widening uint32 -> uint256 therefore fails, as does uint256[] -> mapping.

3. No removals / no collisions. Variables cannot disappear (the gap rule
   below excepted), and the new version may not introduce a non-gap variable
   on top of an existing variable's slot.

4. Append-only additions. New variables may only use slots strictly greater
   than every previously used slot, OR consume space from a recognised
   reserved gap whose head slot already belonged to a gap array.

5. Reserved-gap evolution. A fixed-size array named like a gap
   (``__gap``, ``gap``, ``__xxxGap`` ...) may shrink; its freed tail space is
   considered reserved and usable only by new variables appended in the
   same contract. A gap may never move or grow backwards.

Inheritance/base-class changes are handled structurally: we compare the
sequence of storage entries slot by slot, so swapping a base contract for an
incompatible one (e.g. widening a base field) is caught by rules 1-2 even
though solc attributes inherited entries to the leaf contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

_GAP_RE = re.compile(r"^__[A-Za-z0-9_]*[Gg]ap\d*$|^gap\d*$|^__gap\d*$")


def is_gap_label(label: str) -> bool:
    """Recognise reserved storage-gap names such as __gap / __baseGap / gap0."""
    return bool(_GAP_RE.match(label))


@dataclass(frozen=True)
class TypeId:
    """Encoding-relevant identity of a Solidity storage type."""

    encoding: str
    label: str
    number_of_bytes: str
    base: Optional["TypeId"] = None
    key: Optional["TypeId"] = None
    value: Optional["TypeId"] = None
    members: Optional[Tuple[Tuple[str, "TypeId"], ...]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class Var:
    label: str
    slot: int
    offset: int
    type_id: TypeId
    type_name: str
    encoding: str
    bytes_used: int  # bytes occupied by this variable inside its slot region
    is_gap: bool
    contract: str = ""

    def head(self) -> int:
        """First slot occupied (fixed arrays span several slots)."""
        return self.slot

    def span_slots(self) -> int:
        n = int(self.type_id.number_of_bytes)
        return max(1, (self.offset + n + 31) // 32)

    def tail_slot_exclusive(self) -> int:
        return self.slot + self.span_slots()


@dataclass
class Finding:
    severity: str  # "error" | "warning" | "info"
    code: str
    message: str
    label: Optional[str] = None
    old_slot: Optional[int] = None
    new_slot: Optional[int] = None
    old_type: Optional[str] = None
    new_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class CheckResult:
    compatible: bool
    errors: List[Finding] = field(default_factory=list)
    warnings: List[Finding] = field(default_factory=list)
    infos: List[Finding] = field(default_factory=list)
    added: List[Dict[str, Any]] = field(default_factory=list)
    removed: List[Dict[str, Any]] = field(default_factory=list)
    changed: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "compatible": self.compatible,
            "summary": {
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "added": len(self.added),
                "removed": len(self.removed),
                "changed": len(self.changed),
            },
            "errors": [f.to_dict() for f in self.errors],
            "warnings": [f.to_dict() for f in self.warnings],
            "infos": [f.to_dict() for f in self.infos],
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed,
        }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _build_type_id(
    type_key: str, types: Dict[str, Dict[str, Any]], memo: Dict[str, TypeId]
) -> TypeId:
    if type_key in memo:
        return memo[type_key]
    t = types[type_key]
    tid = TypeId(
        encoding=t.get("encoding", ""),
        label=t.get("label", ""),
        number_of_bytes=t.get("numberOfBytes", "0"),
    )
    memo[type_key] = tid  # break recursion for structs
    base = t.get("base")
    key = t.get("key")
    val = t.get("value")
    members = t.get("members")
    object.__setattr__(
        tid, "base", _build_type_id(base, types, memo) if base else None
    )
    object.__setattr__(
        tid, "key", _build_type_id(key, types, memo) if key else None
    )
    object.__setattr__(
        tid, "value", _build_type_id(val, types, memo) if val else None
    )
    if members:
        object.__setattr__(
            tid,
            "members",
            tuple((m["label"], _build_type_id(m["type"], types, memo)) for m in members),
        )
    return tid


def parse_layout(doc: Any, contract_name: Optional[str] = None) -> Tuple[List[Var], Dict[str, Any]]:
    """Accept either a raw solc/forge artifact JSON or an already-unwrapped
    ``{"contract": ..., "storageLayout": {...}}`` document.

    Returns (variables, meta).
    """
    if not isinstance(doc, dict):
        raise ValueError("layout document must be a JSON object")

    # Unwrap common wrappers.
    if "storageLayout" in doc and isinstance(doc["storageLayout"], dict):
        sl = doc["storageLayout"]
        meta_contract = doc.get("contract") or contract_name or ""
        abi = doc.get("abi")
        linear = doc.get("linearizedBaseContracts")
    elif "storage" in doc and "types" in doc:
        sl = doc
        meta_contract = contract_name or ""
        abi = None
        linear = None
    elif "storage" in doc:  # types possibly elsewhere
        sl = doc
        meta_contract = contract_name or ""
        abi = None
        linear = None
    else:
        # forge artifact: { abi, storageLayout, ... }
        raise ValueError(
            "cannot find storageLayout/storage in document; pass a solc/forge artifact"
        )

    storage = sl.get("storage", [])
    types = sl.get("types", {})
    if not isinstance(storage, list):
        raise ValueError("storageLayout.storage must be a list")

    memo: Dict[str, TypeId] = {}
    vars_: List[Var] = []
    for s in storage:
        tk = s["type"]
        tid = _build_type_id(tk, types, memo)
        nbytes = int(tid.number_of_bytes)
        v = Var(
            label=s["label"],
            slot=int(s["slot"]),
            offset=int(s["offset"]),
            type_id=tid,
            type_name=tid.label,
            encoding=tid.encoding,
            bytes_used=nbytes,
            is_gap=is_gap_label(s["label"]),
            contract=s.get("contract", meta_contract),
        )
        vars_.append(v)
    meta = {"contract": meta_contract, "abi": abi, "linearizedBaseContracts": linear}
    return vars_, meta


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _overlap(a_slot: int, a_span: int, b_slot: int, b_span: int) -> bool:
    return a_slot < b_slot + b_span and b_slot < a_slot + a_span


def _type_change_reason(old: Var, new: Var) -> Optional[str]:
    """Human-readable explanation when two same-named vars have incompatible types."""
    o, n = old.type_id, new.type_id
    if o.encoding != n.encoding:
        return f"encoding changed {o.encoding} -> {n.encoding}"
    if o.number_of_bytes != n.number_of_bytes:
        return f"size changed {o.label} ({o.number_of_bytes}B) -> {n.label} ({n.number_of_bytes}B)"
    if o.encoding == "dynamic_array" and o.base != n.base:
        return f"array element type changed {o.base.label} -> {n.base.label}"
    if o.encoding == "mapping" and (o.key != n.key or o.value != n.value):
        return (
            f"mapping signature changed {o.key.label} => {o.value.label} -> "
            f"{n.key.label} => {n.value.label}"
        )
    if o.members != n.members:
        return "struct members changed"
    if o != n:
        return f"type changed {o.label} -> {n.label}"
    return None


def check_layouts(
    old_doc: Any,
    new_doc: Any,
    *,
    old_contract: Optional[str] = None,
    new_contract: Optional[str] = None,
) -> CheckResult:
    """Compare two compiler storage-layout documents.

    Returns a CheckResult; ``compatible`` is True only when no blocking
    ``error`` findings were produced.
    """
    old_vars, old_meta = parse_layout(old_doc, old_contract)
    new_vars, new_meta = parse_layout(new_doc, new_contract)

    res = CheckResult(compatible=True)

    old_by_label: Dict[str, Var] = {v.label: v for v in old_vars}
    new_by_label: Dict[str, Var] = {v.label: v for v in new_vars}
    labels_old = set(old_by_label)
    labels_new = set(new_by_label)

    shared = labels_old & labels_new
    added_labels = labels_new - labels_old
    removed_labels = labels_old - labels_new

    # ---- Rule 1 & 2: shared labels keep slot/offset and type --------------
    # Gap variables are exempt here: their slot/type evolution is validated
    # structurally by Rule 5 (a gap may shrink and have its head consumed).
    for lbl in sorted(shared):
        o, n = old_by_label[lbl], new_by_label[lbl]
        if o.is_gap and n.is_gap:
            continue
        changes: Dict[str, Any] = {"label": lbl}
        moved = False
        if o.slot != n.slot:
            moved = True
            changes["slot"] = [o.slot, n.slot]
            res.errors.append(
                Finding(
                    "error",
                    "SLOT_MOVED",
                    f"variable '{lbl}' moved from slot {o.slot} to slot {n.slot}; "
                    "existing storage bytes would be interpreted as the wrong variable",
                    label=lbl,
                    old_slot=o.slot,
                    new_slot=n.slot,
                )
            )
        if o.offset != n.offset:
            moved = True
            changes["offset"] = [o.offset, n.offset]
            res.errors.append(
                Finding(
                    "error",
                    "OFFSET_MOVED",
                    f"variable '{lbl}' offset changed {o.offset} -> {n.offset} "
                    f"within slot {n.slot}; packed storage would be misread",
                    label=lbl,
                    old_slot=o.slot,
                    new_slot=n.slot,
                )
            )
        reason = _type_change_reason(o, n)
        if reason is not None:
            changes["type"] = [o.type_name, n.type_name]
            code = "DYNAMIC_TYPE_CHANGED" if {o.encoding, n.encoding} & {
                "dynamic_array",
                "mapping",
                "bytes",
            } else "TYPE_CHANGED"
            res.errors.append(
                Finding(
                    "error",
                    code,
                    f"variable '{lbl}' {reason}; type/layout-sensitive change is unsafe",
                    label=lbl,
                    old_type=o.type_name,
                    new_type=n.type_name,
                    old_slot=o.slot,
                    new_slot=n.slot,
                )
            )
        if moved or "type" in changes:
            res.changed.append(changes)

    # ---- Rule 3a: removals -------------------------------------------------
    # A removed non-gap variable leaves stale data behind and usually means a
    # later variable will collide with it (reported separately).
    for lbl in sorted(removed_labels):
        v = old_by_label[lbl]
        entry = {"label": lbl, "slot": v.slot, "offset": v.offset, "type": v.type_name}
        if v.is_gap:
            # Gap disappearing is allowed only when its slots are covered by
            # another gap in the new layout (checked in the collision pass).
            entry["was_gap"] = True
            res.removed.append(entry)
            res.infos.append(
                Finding("info", "GAP_REMOVED", f"reserved gap '{lbl}' removed", label=lbl)
            )
        else:
            res.removed.append(entry)
            res.errors.append(
                Finding(
                    "error",
                    "VAR_REMOVED",
                    f"variable '{lbl}' ({v.type_name} @ slot {v.slot}) was removed; "
                    "storage variables cannot be deleted in an upgrade (deprecate or keep a gap)",
                    label=lbl,
                    old_slot=v.slot,
                    old_type=v.type_name,
                )
            )

    # ---- Rule 4: additions must be append-only or gap-consuming ----------
    old_non_gap_tail = max(
        (v.tail_slot_exclusive() for v in old_vars if not v.is_gap), default=0
    )

    added_vars = [new_by_label[l] for l in sorted(added_labels)]
    # Map occupied regions of the OLD layout so new vars can be classified as
    # "append" / "gap-consume" / "collision".
    for nv in added_vars:
        entry = {
            "label": nv.label,
            "slot": nv.slot,
            "offset": nv.offset,
            "type": nv.type_name,
        }
        if nv.is_gap:
            entry["is_gap"] = True
            res.added.append(entry)
            # A new gap may only sit in previously-unused space.
            collides_with = _find_collisions(nv, [v for v in old_vars if not v.is_gap])
            if collides_with:
                res.errors.append(
                    Finding(
                        "error",
                        "GAP_OVER_DATA",
                        f"new gap '{nv.label}' overlaps existing variable(s) "
                        f"{', '.join(c.label for c in collides_with)}",
                        label=nv.label,
                        new_slot=nv.slot,
                    )
                )
            else:
                res.infos.append(
                    Finding("info", "GAP_ADDED", f"new reserved gap '{nv.label}'", label=nv.label)
                )
            continue

        # Non-gap addition: check overlap with any OLD non-gap variable.
        colliding = _find_collisions(nv, [v for v in old_vars if not v.is_gap])
        if colliding:
            c = colliding[0]
            res.added.append(entry)
            res.errors.append(
                Finding(
                    "error",
                    "SLOT_COLLISION",
                    f"new variable '{nv.label}' ({nv.type_name} @ slot {nv.slot}) overlaps "
                    f"existing variable '{c.label}' ({c.type_name} @ slot {c.slot}); "
                    "new fields must be appended after the last used slot or placed in a gap",
                    label=nv.label,
                    new_slot=nv.slot,
                )
            )
            continue

        # Does it consume a previously reserved gap region?
        consumed_gaps = _find_collisions(nv, [v for v in old_vars if v.is_gap])
        if consumed_gaps:
            for g in consumed_gaps:
                # Only consumption from the TAIL of a gap is safe: the new
                # variable's slot must be >= the last slot of the gap region
                # that is still needed, i.e. the matching new gap (if any)
                # shrinks in place. That pairing is validated below.
                res.infos.append(
                    Finding(
                        "info",
                        "GAP_CONSUMED",
                        f"new variable '{nv.label}' occupies slot {nv.slot} inside "
                        f"reserved gap '{g.label}'; accepted only if that gap shrinks in place",
                        label=nv.label,
                        new_slot=nv.slot,
                    )
                )
            entry["consumed_gap"] = consumed_gaps[0].label
            res.added.append(entry)
        elif nv.slot >= old_non_gap_tail:
            res.added.append(entry)
            res.infos.append(
                Finding(
                    "info",
                    "VAR_APPENDED",
                    f"new variable '{nv.label}' appended at slot {nv.slot}",
                    label=nv.label,
                    new_slot=nv.slot,
                )
            )
        else:
            # Lands on a slot below the tail but doesn't overlap anything —
            # can only be a hole left by a removed gap. Accept, but warn.
            res.added.append(entry)
            res.warnings.append(
                Finding(
                    "warning",
                    "VAR_IN_OLD_HOLE",
                    f"new variable '{nv.label}' uses previously-unreserved slot {nv.slot}; "
                    "make sure this slot was a gap in every deployed version",
                    label=nv.label,
                    new_slot=nv.slot,
                )
            )

    # ---- Rule 5: gap pairing (shrink-in-place) -----------------------------
    _validate_gap_evolution(old_vars, new_vars, res)

    # ---- Packed-slot sanity within each version ----------------------------
    _check_internal_packing(old_vars, side="old", res=res)
    _check_internal_packing(new_vars, side="new", res=res)

    # ---- Inheritance metadata (best-effort, warnings only) -----------------
    old_linear = old_meta.get("linearizedBaseContracts")
    new_linear = new_meta.get("linearizedBaseContracts")
    if old_linear and new_linear and old_linear != new_linear:
        res.warnings.append(
            Finding(
                "warning",
                "INHERITANCE_CHANGED",
                "linearized base-contract list differs between versions; "
                "storage safety was evaluated structurally slot-by-slot",
            )
        )

    res.compatible = not res.errors
    return res


def _find_collisions(nv: Var, candidates: List[Var]) -> List[Var]:
    out = []
    ns, nspan = nv.slot, nv.span_slots()
    for c in candidates:
        # Same-slot packing is fine when byte ranges don't intersect.
        if c.slot == nv.slot and c.span_slots() == 1 and nspan == 1:
            if not _byte_ranges_overlap(c, nv):
                continue
        if _overlap(c.slot, c.span_slots(), ns, nspan):
            out.append(c)
    return out


def _byte_ranges_overlap(a: Var, b: Var) -> bool:
    a_lo, a_hi = a.offset, a.offset + max(1, int(a.type_id.number_of_bytes))
    b_lo, b_hi = b.offset, b.offset + max(1, int(b.type_id.number_of_bytes))
    return a_lo < b_hi and b_lo < a_hi


def _validate_gap_evolution(old_vars: List[Var], new_vars: List[Var], res: CheckResult) -> None:
    old_gaps = {v.label: v for v in old_vars if v.is_gap}
    new_gaps = {v.label: v for v in new_vars if v.is_gap}

    for label, og in old_gaps.items():
        ng = new_gaps.get(label)
        if ng is None:
            # The whole gap vanished. Every slot it covered must now be used
            # exclusively by new variables (tail consumption) — otherwise
            # shrinking was not "in place".
            covered_by_new = [
                v
                for v in new_vars
                if not v.is_gap and _overlap(v.slot, v.span_slots(), og.slot, og.span_slots())
            ]
            if not covered_by_new:
                res.errors.append(
                    Finding(
                        "error",
                        "GAP_MOVED",
                        f"reserved gap '{label}' disappeared without replacement fields; "
                        "keep the gap or shrink it explicitly",
                        label=label,
                        old_slot=og.slot,
                    )
                )
            else:
                # New vars must consume only the TAIL of the gap: nothing may
                # sit below the gap head that wasn't there, and each new var
                # must be within the old gap region.
                for v in covered_by_new:
                    if v.slot < og.slot:
                        res.errors.append(
                            Finding(
                                "error",
                                "GAP_HEAD_USED",
                                f"variable '{v.label}' at slot {v.slot} uses the head "
                                f"region of removed gap '{label}' starting at {og.slot}",
                                label=v.label,
                            )
                        )
            continue
        # Gap retained: it must not grow, and its head may only move forward
        # when the vacated slots are fully consumed by NEW variables
        # (reserved-gap consumption).
        if ng.span_slots() > og.span_slots():
            res.errors.append(
                Finding(
                    "error",
                    "GAP_GREW",
                    f"gap '{label}' grew {og.span_slots()} -> {ng.span_slots()} slots; "
                    "a gap cannot expand backwards over previously used storage",
                    label=label,
                    old_slot=og.slot,
                    new_slot=ng.slot,
                )
            )
        if ng.slot != og.slot:
            if ng.slot < og.slot:
                res.errors.append(
                    Finding(
                        "error",
                        "GAP_MOVED",
                        f"gap '{label}' head moved backwards from slot {og.slot} to {ng.slot}",
                        label=label,
                        old_slot=og.slot,
                        new_slot=ng.slot,
                    )
                )
            else:
                # Head moved forward: every vacated slot [og.slot, ng.slot)
                # must be covered by variables that did not exist before.
                old_labels = {v.label for v in old_vars}
                vacated = set(range(og.slot, ng.slot))
                covered = set()
                for v in new_vars:
                    if v.is_gap or v.label in old_labels:
                        continue
                    for s in range(v.slot, v.tail_slot_exclusive()):
                        if s in vacated:
                            covered.add(s)
                if covered == vacated:
                    res.infos.append(
                        Finding(
                            "info",
                            "GAP_CONSUMED",
                            f"gap '{label}' head moved {og.slot} -> {ng.slot}; vacated "
                            f"slot(s) {sorted(vacated)} consumed by new variables",
                            label=label,
                        )
                    )
                else:
                    res.errors.append(
                        Finding(
                            "error",
                            "GAP_MOVED",
                            f"gap '{label}' head moved from slot {og.slot} to {ng.slot} "
                            f"but vacated slot(s) {sorted(vacated - covered)} are not "
                            "consumed by new variables",
                            label=label,
                            old_slot=og.slot,
                            new_slot=ng.slot,
                        )
                    )
        elif ng.span_slots() < og.span_slots():
            res.infos.append(
                Finding(
                    "info",
                    "GAP_SHRUNK",
                    f"gap '{label}' shrunk {og.span_slots()} -> {ng.span_slots()} slots "
                    "(freed tail must hold new appended variables)",
                    label=label,
                )
            )
            # Freed tail must actually be filled by new variables, otherwise
            # the layout just wasted reserved space (safe, but flag info).
            freed_lo = ng.tail_slot_exclusive()
            freed_hi = og.tail_slot_exclusive()
            fillers = [
                v
                for v in new_vars
                if not v.is_gap
                and v.label not in {x.label for x in old_vars}
                and v.slot >= freed_lo
                and v.slot < freed_hi
            ]
            if not fillers:
                res.infos.append(
                    Finding(
                        "info",
                        "GAP_SHRUNK_UNUSED",
                        f"gap '{label}' shrunk but its freed tail [{freed_lo},{freed_hi}) "
                        "holds no new variable",
                        label=label,
                    )
                )


def _check_internal_packing(vars_: List[Var], side: str, res: CheckResult) -> None:
    """Report two non-gap variables overlapping at the byte level."""
    by_slot: Dict[int, List[Var]] = {}
    for v in vars_:
        by_slot.setdefault(v.slot, []).append(v)
    for slot, group in by_slot.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a.is_gap or b.is_gap:
                    continue
                if _byte_ranges_overlap(a, b):
                    res.errors.append(
                        Finding(
                            "error",
                            "INTERNAL_OVERLAP",
                            f"{side} layout: '{a.label}' and '{b.label}' overlap inside slot {slot}",
                            label=a.label,
                        )
                    )
