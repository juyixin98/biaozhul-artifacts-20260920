#!/usr/bin/env python3
"""
Independent reference implementation + fixture generator for cross-checks.

The service under test is TypeScript; this file is Python 3 using only the
standard library (json, copy, random). It implements RFC 6902 for the six
supported ops *independently* of src/kernel.ts, applies random patches, and
emits scenarios with concrete expected results (or concrete expected failure
category + failing operation index) to test/fixtures/crosscheck-generated.json.

It does NOT import or shell out to anything in src/ — the oracle cannot be
contaminated by the implementation being validated.

Usage: python3 scripts/generate-crosscheck.py [seed] [count]
"""

import copy
import json
import os
import random
import sys

# Failure category strings MUST match src/kernel.ts KernelFailureCategory.
TEST_FAILED = "TEST_FAILED"
POINTER_ERROR = "POINTER_ERROR"
MOVE_TARGET_DESCENDANT = "MOVE_TARGET_DESCENDANT"
ROOT_LOCATION_INVALID = "ROOT_LOCATION_INVALID"


class RefFailure(Exception):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


# ---------- RFC 6901 (independent implementation) ----------

def parse_pointer(pointer):
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise RefFailure(POINTER_ERROR, "pointer must start with /")
    return [t.replace("~1", "/").replace("~0", "~")
            for t in pointer[1:].split("/")]


def format_pointer(segments):
    return "".join(
        "/" + s.replace("~", "~0").replace("/", "~1") for s in segments
    )


def array_index(token, length, *, allow_dash, allow_end):
    if token == "-":
        if not allow_dash:
            raise RefFailure(POINTER_ERROR, "'-' cannot be read")
        return length
    if not (token.isdigit() and (token == "0" or not token.startswith("0"))):
        raise RefFailure(POINTER_ERROR, f"bad array index {token!r}")
    index = int(token)
    bound = length if allow_end else length - 1
    if index > bound:
        raise RefFailure(POINTER_ERROR, f"index {index} out of bounds (len {length})")
    return index


def step_into(current, token):
    if isinstance(current, list):
        return current[array_index(token, len(current), allow_dash=False, allow_end=False)]
    if isinstance(current, dict):
        if token not in current:
            raise RefFailure(POINTER_ERROR, f"missing member {token!r}")
        return current[token]
    raise RefFailure(POINTER_ERROR, "cannot traverse non-container")


def locate_parent(doc, segments):
    if not segments:
        raise RefFailure(POINTER_ERROR, "empty pointer has no parent")
    current = doc
    for token in segments[:-1]:
        current = step_into(current, token)
    if not isinstance(current, (list, dict)):
        raise RefFailure(POINTER_ERROR, "parent is not a container")
    return current, segments[-1]


def resolve(doc, pointer):
    segments = parse_pointer(pointer)
    if not segments:
        return doc
    parent, key = locate_parent(doc, segments)
    if isinstance(parent, list):
        index = array_index(key, len(parent), allow_dash=False, allow_end=False)
        return parent[index]
    if key not in parent:
        raise RefFailure(POINTER_ERROR, f"missing member {key!r}")
    return parent[key]


# ---------- RFC 6902 ops (independent implementation) ----------

def deep_equal(a, b):
    if type(a) is not type(b):
        # JSON booleans are ints in Python; normalize explicitly.
        if isinstance(a, bool) or isinstance(b, bool):
            return False
        return False
    if isinstance(a, dict):
        return set(a.keys()) == set(b.keys()) and all(
            deep_equal(a[k], b[k]) for k in a
        )
    if isinstance(a, list):
        return len(a) == len(b) and all(deep_equal(x, y) for x, y in zip(a, b))
    return a == b


def ref_add(doc, segments, value):
    if not segments:
        return copy.deepcopy(value)
    parent, key = locate_parent(doc, segments)
    if isinstance(parent, list):
        index = array_index(key, len(parent), allow_dash=True, allow_end=True)
        parent.insert(index, copy.deepcopy(value))
    else:
        parent[key] = copy.deepcopy(value)
    return doc


def ref_remove(doc, segments):
    if not segments:
        raise RefFailure(ROOT_LOCATION_INVALID, "cannot remove root")
    parent, key = locate_parent(doc, segments)
    if isinstance(parent, list):
        index = array_index(key, len(parent), allow_dash=False, allow_end=False)
        del parent[index]
    else:
        if key not in parent:
            raise RefFailure(POINTER_ERROR, f"cannot remove missing key {key!r}")
        del parent[key]
    return doc


def ref_replace(doc, segments, value):
    resolve(doc, format_pointer(segments))  # existence check
    if not segments:
        return copy.deepcopy(value)
    parent, key = locate_parent(doc, segments)
    if isinstance(parent, list):
        index = array_index(key, len(parent), allow_dash=False, allow_end=False)
        parent[index] = copy.deepcopy(value)
    else:
        parent[key] = copy.deepcopy(value)
    return doc


def ref_move(doc, from_segments, to_segments):
    if not from_segments:
        raise RefFailure(ROOT_LOCATION_INVALID, "cannot move root")
    from_ptr = format_pointer(from_segments)
    to_ptr = format_pointer(to_segments)
    if from_ptr == to_ptr or to_ptr.startswith(from_ptr + "/"):
        raise RefFailure(MOVE_TARGET_DESCENDANT, "move into self/descendant")
    parent, key = locate_parent(doc, from_segments)
    if isinstance(parent, list):
        index = array_index(key, len(parent), allow_dash=False, allow_end=False)
        removed = parent.pop(index)
    else:
        if key not in parent:
            raise RefFailure(POINTER_ERROR, "move source missing")
        removed = parent.pop(key)
    return ref_add(doc, to_segments, removed)


def ref_copy(doc, from_segments, to_segments):
    value = resolve(doc, format_pointer(from_segments))
    return ref_add(doc, to_segments, value)


def ref_test(doc, segments, expected):
    try:
        actual = resolve(doc, format_pointer(segments))
    except RefFailure as failure:
        raise RefFailure(TEST_FAILED, f"test target missing: {failure}")
    if not deep_equal(actual, expected):
        raise RefFailure(TEST_FAILED, "values differ")
    return doc


def apply_reference(document, operations):
    doc = copy.deepcopy(document)
    for index, op in enumerate(operations):
        segments = parse_pointer(op["path"])
        kind = op["op"]
        try:
            if kind == "add":
                doc = ref_add(doc, segments, op["value"])
            elif kind == "remove":
                doc = ref_remove(doc, segments)
            elif kind == "replace":
                doc = ref_replace(doc, segments, op["value"])
            elif kind == "move":
                doc = ref_move(doc, parse_pointer(op["from"]), segments)
            elif kind == "copy":
                doc = ref_copy(doc, parse_pointer(op["from"]), segments)
            elif kind == "test":
                doc = ref_test(doc, segments, op["value"])
            else:
                raise RefFailure(POINTER_ERROR, f"unknown op {kind}")
        except RefFailure as failure:
            return {"ok": False, "failedAtIndex": index, "category": failure.category,
                    "message": str(failure)}
    return {"ok": True, "result": doc}


# ---------- Random scenario generation ----------

def enumerate_pointers(doc, segments=None, out=None):
    """All readable pointers plus, for arrays, the '-' append pointer."""
    if segments is None:
        segments, out = [], []
    out.append(format_pointer(segments))
    if isinstance(doc, dict):
        for key, value in doc.items():
            enumerate_pointers(value, segments + [key], out)
    elif isinstance(doc, list):
        for i, value in enumerate(doc):
            enumerate_pointers(value, segments + [str(i)], out)
        out.append(format_pointer(segments + ["-"]))
    return out


def random_value(rng):
    kind = rng.randrange(7)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.randrange(-50, 50)
    if kind == 3:
        return rng.choice(["alpha", "a/b", "a~b", "", "x"])
    if kind == 4:
        return [rng.randrange(0, 9) for _ in range(rng.randrange(0, 3))]
    if kind == 5:
        return {rng.choice(["k", "a/b", ""]): rng.randrange(0, 9)}
    return {"nested": {"z": rng.randrange(0, 9)}}


TEMPLATES = [
    {"foo": {"bar": [1, 2, 3], "baz": "qux"}, "n": 0},
    [10, 20, [30, 31], {"k": "v"}],
    {"a/b": {"a~b": [1, {"": "empty"}]}, "list": []},
    {"": {"x": [True, False, None]}, "matrix": [[1], [2, 3]]},
    {"items": [{"id": 1}, {"id": 2}], "count": 2, "meta": {"tags": ["a"]}},
]


def make_operation(rng, doc, force_fail=None):
    pointers = enumerate_pointers(doc)
    readable = [p for p in pointers if not p.endswith("/-") and p != "-"]
    kind = rng.choice(["add", "remove", "replace", "move", "copy", "test"])

    if kind == "test":
        path = rng.choice(readable)
        if force_fail == "test" or (force_fail is None and rng.random() < 0.4):
            return {"op": "test", "path": path, "value": random_value(rng)}
        return {"op": "test", "path": path, "value": copy.deepcopy(resolve(doc, path))}

    if kind == "add":
        return {"op": "add", "path": rng.choice(pointers), "value": random_value(rng)}
    if kind == "replace":
        return {"op": "replace", "path": rng.choice(readable), "value": random_value(rng)}
    if kind == "remove":
        candidates = [p for p in readable if p != ""]
        return {"op": "remove", "path": rng.choice(candidates) if candidates else "/nope"}
    if kind == "copy":
        return {"op": "copy", "from": rng.choice(readable), "path": rng.choice(pointers)}

    # move
    from_ptr = rng.choice([p for p in readable if p != ""])
    if force_fail == "move_desc" or (force_fail is None and rng.random() < 0.25):
        # Aim into the source subtree or at itself.
        target = from_ptr + rng.choice(["", "/child", "/0"])
        return {"op": "move", "from": from_ptr, "path": target}
    return {"op": "move", "from": from_ptr, "path": rng.choice(pointers)}


def generate(seed, count):
    rng = random.Random(seed)
    scenarios = []
    for i in range(count):
        doc = copy.deepcopy(rng.choice(TEMPLATES))
        op_count = rng.choice([1, 1, 2, 2, 3, 4])
        operations = []
        for _ in range(op_count):
            live = apply_reference(doc, operations)
            if not live["ok"]:
                break  # earlier op already failed; stop generating
            operations.append(make_operation(rng, live["result"]))
        outcome = apply_reference(doc, operations)
        scenarios.append({
            "id": f"gen-{seed}-{i:04d}",
            "document": doc,
            "operations": operations,
            "expected": outcome,
        })
    return scenarios


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 6902
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 140
    scenarios = generate(seed, count)
    here = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(here, "..", "test", "fixtures", "crosscheck-generated.json")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "_meta": {
                    "generator": "scripts/generate-crosscheck.py",
                    "oracle": "independent Python stdlib RFC 6902 reference",
                    "seed": seed,
                    "count": len(scenarios),
                },
                "scenarios": scenarios,
            },
            handle,
            indent=2,
            ensure_ascii=False,
        )
        handle.write("\n")
    failures = sum(1 for s in scenarios if not s["expected"]["ok"])
    print(f"wrote {len(scenarios)} scenarios ({failures} expected failures) -> {out_path}")


if __name__ == "__main__":
    main()
