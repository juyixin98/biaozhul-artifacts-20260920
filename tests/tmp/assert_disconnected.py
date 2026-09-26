import json,sys
r=json.load(sys.stdin)
assert r["status"]=="feasible"
a=r["assignment"]
# two disconnected components: {a,b} and {c}; every var must be assigned
assert set(a)=={"a","b","c"}
assert a["a"]-a["b"]<=4
assert min(a.values())==0
sys.exit(0)
