import json,sys
r=json.load(sys.stdin)
assert r["status"]=="feasible"
assert r["minimal_infeasible_candidates"]==[]
sys.exit(0)
