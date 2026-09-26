import json,sys
r=json.load(sys.stdin)
assert r["satisfied"] is True and r["violated"]==[]
sys.exit(0)
