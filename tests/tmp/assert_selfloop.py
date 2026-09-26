import json,sys
r=json.load(sys.stdin)
assert r["status"]=="infeasible"
assert r["negative_cycle"]["constraint_ids"]==["s1"]
assert r["negative_cycle"]["total_weight"]==-1
sys.exit(0)
