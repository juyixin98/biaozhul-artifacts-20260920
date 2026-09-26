import json,sys
r=json.load(sys.stdin)
assert "error" in r, r
sys.exit(0)
