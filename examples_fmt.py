#!/usr/bin/env python3
"""Tiny JSON pretty-printers used by examples.sh.

Reads one JSON document on stdin and prints a human-readable view selected by
the first positional argument:
  field k.k...        print scalar/leaf at a dotted path
  nodes               one line per node (id/role/term/commit/kv)
  leader-nodes        current leader plus per-node role/term
  ce                  counterexample verdict + violations
  enum                enumeration counters
"""
import json
import sys


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "field"
    data = json.load(sys.stdin)

    if mode == "field":
        x = data
        for key in sys.argv[2:]:
            if isinstance(x, list):
                x = x[int(key)]
            else:
                x = x[key]
        print(json.dumps(x) if isinstance(x, (dict, list)) else x)
    elif mode == "nodes":
        for n in data["nodes"]:
            print("node {id}: role={role} term={term} commitIndex={ci} kv={kv}".format(
                id=n["id"], role=n["role"], term=n["term"],
                ci=n["commitIndex"], kv=n["kv"]))
    elif mode == "leader-nodes":
        print("leader:", data["leader"])
        for n in data["nodes"]:
            print("node {id}: role={role} term={term}".format(
                id=n["id"], role=n["role"], term=n["term"]))
    elif mode == "ce":
        print("variant:", data["scenario"]["variant"], "ok:", data["ok"])
        for v in data["violations"]:
            print(" VIOLATION:", v["invariant"], "-", v["detail"])
    elif mode == "enum":
        elapsed = int(data["elapsed"]) / 1e9
        print("ran {e} exhaustive + {f} fuzz traces; counterexamples: {n}; "
              "elapsed: {t:.3f}s".format(e=data["exhaustiveRan"],
                                         f=data["fuzzRan"],
                                         n=len(data["counterexamples"]),
                                         t=elapsed))
    else:
        sys.exit("unknown mode: " + mode)


if __name__ == "__main__":
    main()
