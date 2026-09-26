import json,sys
r=json.load(sys.stdin)
assert r["satisfied"] is False
assert set(r["violated"])=={"c1","c4"}, r
sys.exit(0)
