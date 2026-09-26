import json,sys
r=json.load(sys.stdin)
assert r["status"]=="feasible"
a=r["assignment"]
# zero cycle forces exact differences: a-b==5, b-c==2, c-a==-7
assert a["a"]-a["b"]==5 and a["b"]-a["c"]==2 and a["c"]-a["a"]==-7, a
sys.exit(0)
