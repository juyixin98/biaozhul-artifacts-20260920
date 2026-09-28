#!/usr/bin/env bash
# Example calls against a locally running server.
# Start first:  uvicorn app.api:app --reload --port 8000
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8000}

echo "1) hand-computable 2x2 DID"
curl -s "$BASE/api/v1/did" -H 'Content-Type: application/json' -d '{
  "request_id": "demo-handcalc",
  "pre_period": 1, "post_period": 2,
  "observations": [
    {"object_id":"T1","period":1,"y":10,"treated_group":true,"treated_this_period":false},
    {"object_id":"T1","period":2,"y":14,"treated_group":true,"treated_this_period":true},
    {"object_id":"T2","period":1,"y":12,"treated_group":true,"treated_this_period":false},
    {"object_id":"T2","period":2,"y":15,"treated_group":true,"treated_this_period":true},
    {"object_id":"C1","period":1,"y":8,"treated_group":false,"treated_this_period":false},
    {"object_id":"C1","period":2,"y":9,"treated_group":false,"treated_this_period":false},
    {"object_id":"C2","period":1,"y":10,"treated_group":false,"treated_this_period":false},
    {"object_id":"C2","period":2,"y":12,"treated_group":false,"treated_this_period":false}
  ]
}' | python3 -m json.tool

echo "2) single-cohort event study"
curl -s "$BASE/api/v1/event-study" -H 'Content-Type: application/json' -d '{
  "request_id":"demo-event",
  "min_event_time":-2,"max_event_time":1,
  "observations":[
    {"object_id":"T1","period":1,"y":10,"treated_group":true,"treated_this_period":false},
    {"object_id":"T1","period":2,"y":11,"treated_group":true,"treated_this_period":false},
    {"object_id":"T1","period":3,"y":17,"treated_group":true,"treated_this_period":true},
    {"object_id":"T1","period":4,"y":18,"treated_group":true,"treated_this_period":true},
    {"object_id":"C1","period":1,"y":8,"treated_group":false,"treated_this_period":false},
    {"object_id":"C1","period":2,"y":9,"treated_group":false,"treated_this_period":false},
    {"object_id":"C1","period":3,"y":10,"treated_group":false,"treated_this_period":false},
    {"object_id":"C1","period":4,"y":11,"treated_group":false,"treated_this_period":false}
  ]
}' | python3 -m json.tool

echo "3) staggered adoption -> refused"
curl -s "$BASE/api/v1/event-study" -H 'Content-Type: application/json' -d '{
  "request_id":"demo-stagger","min_event_time":-1,"max_event_time":0,
  "observations":[
    {"object_id":"T_EARLY","period":1,"y":10,"treated_group":true,"treated_this_period":false},
    {"object_id":"T_EARLY","period":2,"y":16,"treated_group":true,"treated_this_period":true},
    {"object_id":"T_LATE","period":1,"y":9,"treated_group":true,"treated_this_period":false},
    {"object_id":"T_LATE","period":2,"y":10,"treated_group":true,"treated_this_period":false},
    {"object_id":"T_LATE","period":3,"y":16,"treated_group":true,"treated_this_period":true},
    {"object_id":"C1","period":1,"y":8,"treated_group":false,"treated_this_period":false},
    {"object_id":"C1","period":2,"y":9,"treated_group":false,"treated_this_period":false},
    {"object_id":"C1","period":3,"y":10,"treated_group":false,"treated_this_period":false}
  ]
}' | python3 -m json.tool
