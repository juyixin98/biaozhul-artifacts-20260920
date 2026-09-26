import json,sys
r=json.load(sys.stdin)
assert r["status"]=="infeasible", r
cyc=r["negative_cycle"]
assert cyc["total_weight"]<0
assert set(cyc["constraint_ids"])=={"k1","k2","k3"}, cyc
assert set(r["minimal_infeasible_subset"])=={"k1","k2","k3"}
sys.exit(0)
