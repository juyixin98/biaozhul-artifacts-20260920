"""Bytecode-level analysis: metadata separation and library-slot masking.

Two runtime bytecodes are compared on exactly three disjoint regions:

1. the CBOR auxdata tail (metadata)         — compared field by field;
2. 20-byte library link slots               — position must align exactly,
   contents may differ (placeholder vs deployed address, two addresses);
3. everything else (the actual code)        — must match nibble for nibble.

No other normalization exists. Length changes or any nibble difference in
region 3 is reported as a BODY_DIFF mismatch.
"""

from __future__ import annotations

import re

from . import cbor
from .crypto import keccak256
from .fixtures import placeholder_for_fqn

# 34 hex chars between "__$" and "$__" => 40 nibbles == 20 bytes slot.
PLACEHOLDER_RE = re.compile(r"__\$[0-9a-f]{34}\$__")
SLOT_NIBBLES = 40  # 20 bytes

CONTENT_HASH_KEYS = {"ipfs", "bzzr0", "bzzr1", "keccak256"}


class BytecodeFormat(ValueError):
    pass


_NON_SLOT = re.compile(r"[0-9a-fA-F_$]")


def clean_hex(s: str, what: str = "bytecode") -> str:
    if not isinstance(s, str):
        raise BytecodeFormat(f"{what} is not a string")
    h = s[2:] if s.startswith("0x") else s
    if len(h) % 2:
        raise BytecodeFormat(f"{what} has odd length")
    stripped = PLACEHOLDER_RE.sub("0" * 40, h)
    if any(c not in "0123456789abcdefABCDEF" for c in stripped):
        raise BytecodeFormat(f"{what} is not hex (outside link placeholders)")
    return h.lower()


def has_placeholder(h: str) -> bool:
    return bool(PLACEHOLDER_RE.search(h))


def placeholder_slots(h: str) -> list[tuple[int, str]]:
    """Return ``[(nibble_start, token), ...]`` for every link placeholder."""
    return [(m.start(), m.group(0)) for m in PLACEHOLDER_RE.finditer(h)]


def mask_slots(h: str, slots: list[tuple[int, str]]) -> str:
    """Replace each slot with a fixed non-code marker and blank the tail."""
    out = list(h)
    for start, token in slots:
        for i in range(start, start + len(token)):
            out[i] = "\x00"  # slot marker, distinct from any hex nibble
    return "".join(out)


def split_tail(h: str) -> tuple[str, dict]:
    """Return ``(body_hex, tail_info)``; raises cbor.CBORError on bad tail."""
    parsed = cbor.parse_metadata_tail(h)
    return parsed["body_hex"], parsed


def link_slots_from_refs(link_refs: dict) -> list[dict]:
    """Flatten ``linkReferences`` to ``{path,name,fqn,start_byte,length}``."""
    slots = []
    for path, names in link_refs.items():
        for name, ranges in names.items():
            for r in ranges:
                slots.append({
                    "path": path,
                    "name": name,
                    "fqn": f"{path}:{name}",
                    "start_byte": r["start"],
                    "length": r["length"],
                })
    slots.sort(key=lambda s: s["start_byte"])
    return slots


def compare_runtime(expected_hex: str, observed_hex: str) -> dict:
    """Compare unlinked reference runtime against deployed (linked) runtime.

    Returns a structured result:
      ``ok``              – True iff only library slots / metadata differ
      ``length_ok``
      ``body_diffs``      – nibble offsets differing outside allowed slots
      ``slot_misalign``   – slots whose position differs
      ``slots``           – per-slot evidence
      ``expected_tail`` / ``observed_tail`` / ``tail_diffs``
      ``observed_unresolved`` – placeholders still present on deployed side
    """
    e = clean_hex(expected_hex, "expected runtime")
    o = clean_hex(observed_hex, "observed runtime")

    result: dict = {
        "ok": False,
        "length_ok": len(e) == len(o),
        "body_diffs": [],
        "slot_misalign": [],
        "slots": [],
        "tail_diffs": [],
        "observed_unresolved": [],
    }
    if not result["length_ok"]:
        result["length_diff_nibbles"] = abs(len(e) - len(o))
        return result

    e_body, e_tail = split_tail(e)
    o_body, o_tail = split_tail(o)
    result["expected_tail"] = _render_tail(e_tail)
    result["observed_tail"] = _render_tail(o_tail)
    result["tail_diffs"] = compare_tails(e_tail["cbor_map"], o_tail["cbor_map"])

    e_slots = placeholder_slots(e_body)
    o_slots = placeholder_slots(o_body)
    result["observed_unresolved"] = [t for _, t in o_slots]

    # Reference slots are authoritative for position. The linked (deployed)
    # side normally has no placeholder; its address occupies the same offset.
    # A placeholder left on the deployed side is an unresolved-link defect.
    e_pos = [p for p, _ in e_slots]
    o_pos = [p for p, _ in o_slots]
    extra_on_deployed = sorted(set(o_pos) - set(e_pos))
    if extra_on_deployed:
        result["slot_misalign"] = {"expected": e_pos, "observed": o_pos}
        return result

    # Validate observed slot region: exactly 40 hex chars (a 20-byte address).
    for start in e_pos:
        region = o_body[start:start + SLOT_NIBBLES]
        if len(region) != SLOT_NIBBLES or not all(
                c in "0123456789abcdefABCDEF_$" for c in region):
            result["slot_misalign"] = {"start_byte": start // 2,
                                       "observed_region": region}
            return result

    masked_e = mask_slots(e_body, e_slots)
    masked_o = mask_slots(o_body, [(p, t) for p, t in e_slots])
    diffs = [
        i for i, (a, b) in enumerate(zip(masked_e, masked_o))
        if a != b and a != "\x00" and b != "\x00"
    ]
    result["body_diffs"] = diffs
    result["expected_body_digest"] = keccak256(masked_e.encode()).hex()
    result["observed_body_digest"] = keccak256(masked_o.encode()).hex()

    for start, token in e_slots:
        result["slots"].append({
            "start_byte": start // 2,
            "length": SLOT_NIBBLES // 2,
            "expected": token,
            "observed": o_body[start:start + SLOT_NIBBLES],
        })

    only_allowed = (
        not diffs
        and not result["slot_misalign"]
        and all(d["kind"] == "content-hash" for d in result["tail_diffs"])
    )
    result["ok"] = only_allowed
    return result


def compare_tails(exp_map: dict, obs_map: dict) -> list[dict]:
    diffs = []
    keys = set(exp_map) | set(obs_map)
    for k in sorted(keys):
        ev = exp_map.get(k)
        ov = obs_map.get(k)
        if ev == ov:
            continue
        kind = "content-hash" if k in CONTENT_HASH_KEYS else "other"
        diffs.append({
            "key": k,
            "kind": kind,
            "expected": _tail_val(ev),
            "observed": _tail_val(ov),
        })
    return diffs


def _render_tail(parsed: dict) -> dict:
    return {
        "length_field": parsed["length_field"],
        "fields": {k: _tail_val(v) for k, v in sorted(parsed["cbor_map"].items())},
    }


def _tail_val(v) -> str:
    if isinstance(v, bytes):
        return "0x" + v.hex()
    return v  # type: ignore[return-value]


def validate_declared_slots(link_refs: dict, body_hex: str) -> list[dict]:
    """Cross-check declared linkReference offsets against actual placeholders.

    Returns problems (empty list = OK).
    """
    problems = []
    actual = dict(placeholder_slots(body_hex))
    for slot in link_slots_from_refs(link_refs):
        start = slot["start_byte"] * 2
        length_nib = slot["length"] * 2
        token = body_hex[start:start + length_nib]
        if start not in actual or not PLACEHOLDER_RE.fullmatch(token):
            problems.append({"code": "LINK_REF_BAD", "slot": slot,
                             "found": token[:42]})
            continue
        want = placeholder_for_fqn(slot["fqn"])
        if token != want:
            problems.append({"code": "PLACEHOLDER_BAD_SCHEME", "slot": slot,
                             "found": token, "expected": want})
    # Placeholders with no declared reference are also a defect.
    declared_positions = {s["start_byte"] * 2 for s in link_slots_from_refs(link_refs)}
    for pos, token in placeholder_slots(body_hex):
        if pos not in declared_positions:
            problems.append({"code": "LINK_REF_BAD",
                             "detail": f"undeclared placeholder at byte {pos // 2}"})
    return problems
