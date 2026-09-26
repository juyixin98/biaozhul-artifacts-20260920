import json,sys
r=json.load(sys.stdin)
assert r["status"]=="infeasible"
got=sorted(tuple(sorted(c["constraint_ids"])) for c in r["minimal_infeasible_candidates"])
assert got==[("m1","m2"),("m3","m4")], got
assert r["enumeration"]["performed"] is True
assert r["enumeration"]["complete"] is True
sys.exit(0)
