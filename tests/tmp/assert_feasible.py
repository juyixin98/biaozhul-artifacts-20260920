import json,sys
r=json.load(sys.stdin)
assert r["status"]=="feasible", r
assert r["verification"]["satisfied"] is True
assert r["verification"]["violated"]==[]
a=r["assignment"]
assert min(a.values())==0, "not normalized"
sys.exit(0)
