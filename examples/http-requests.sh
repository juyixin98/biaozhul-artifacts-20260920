# HTTP request samples for `vccsim serve`
#
# Start the server:
#   go run . serve -addr :8080
#
# The POST body is exactly the same scenario JSON accepted by `vccsim run`.

# 1. Health check
curl -s http://localhost:8080/health

# 2. Offline divergent writes -> two concurrent siblings survive convergence
curl -s -X POST http://localhost:8080/run \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "http-offline-writes",
    "seed": 42,
    "nodes": ["A", "B"],
    "network": {"base_delay": 1},
    "events": [
      {"type": "offline", "time": 0, "node": "B"},
      {"type": "write",   "time": 1, "node": "A", "key": "cart", "value": {"items": ["a"]}},
      {"type": "write",   "time": 2, "node": "B", "key": "cart", "value": {"items": ["b"]}},
      {"type": "online",  "time": 3, "node": "B"},
      {"type": "send",    "time": 4, "from": "A", "to": "B", "key": "cart"},
      {"type": "inspect", "time": 6, "node": "B", "key": "cart",
       "expect_siblings": 2, "expect_ids": ["A:1", "B:1"]}
    ]
  }'

# 3. Stale/incomplete merge context is rejected; full-context merge accepted
curl -s -X POST http://localhost:8080/run \
  -H 'Content-Type: application/json' \
  -d @examples/03-stale-context-merge.json
