#!/usr/bin/env python3
"""Generate self-contained BSE1 JSON request samples from schema files.

Run from the repository root:

    python3 examples/build_requests.py

The emitted files are valid requests for `bse -f <file>` and double as
documentation of the request JSON shape.
"""
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent


def load(name):
    with open(HERE / name, encoding="utf-8") as fh:
        return json.load(fh)


def write(name, doc):
    path = HERE / name
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"wrote {path.name}")


v1 = load("schema_person_v1.json")
v2 = load("schema_person_v2.json")

# 1. Encode under v1. Note: email omitted (missing), active explicitly
#    false, and id explicitly 0 to demonstrate presence semantics.
write(
    "req_encode_v1.json",
    {
        "op": "encode",
        "schema": v1,
        "message": {
            "id": 0,
            "name": "Ada Lovelace",
            "tags": ["math", "first-programmer"],
            "score": 3.5,
            "active": False,
            "addr": {"street": "12 Analytical Engine Way", "zip": 1815},
        },
    },
)

# 2. Encode under v2 exercising new fields: packed repeated ints and an
#    unknown-to-v1 field (age, nickname, scores).
write(
    "req_encode_v2.json",
    {
        "op": "encode",
        "schema": v2,
        "message": {
            "id": 7,
            "name": "Grace Hopper",
            "email": "grace@navy.example",
            "tags": ["compiler", "cobol", "nanoseconds"],
            "score": 9.75,
            "active": True,
            "age": 79,
            "scores": [10, 20, -3, 0],
            "nickname": "Amazing Grace",
            "addr": {"street": "1 Navy Way", "zip": 20902, "city": "Arlington"},
        },
    },
)

# 3. Explicit presence wrappers: email present as null-equivalent missing,
#    id explicitly zero, tags present but empty (distinct from missing).
write(
    "req_encode_v1_wrappers.json",
    {
        "op": "encode",
        "schema": v1,
        "message": {
            "id": {"present": 0},
            "name": {"present": "Zero ID Person"},
            "email": {"missing": True},
            "tags": {"values": []},
            "score": {"missing": True},
            "active": {"present": False},
        },
    },
)

# 4. Schema compatibility: v1 (old) vs v2 (new).
write("req_compat_v1_v2.json", {"op": "compat", "old_schema": v1, "new_schema": v2})

print("done")
