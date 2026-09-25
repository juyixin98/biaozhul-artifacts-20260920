# Captured HTTP responses (real server run)

## POST /jobs  (admitted honest job) -> 202
{"status":"admitted","job":{"id":"a","payload":"sleep:300","demand":1,"release_ms":1790177647483,"deadline_ms":1790177649483,"budget_ms":300,"status":"running","start_ms":1790177647483}}

HTTP 202

## POST /jobs  (predicted-infeasible) -> 422, no job field
{"status":"rejected","error":"job \"x\" cannot meet deadline 1790177648291 ms even if started now: budget 1000 ms \u003e time to deadline 799 ms"}

HTTP 422

## POST /jobs/a/cancel -> 200
{"job":{"id":"a","payload":"sleep:300","demand":1,"release_ms":1790177647483,"deadline_ms":1790177649483,"budget_ms":300,"status":"running","start_ms":1790177647483},"status":"canceled"}

HTTP 200

## repeat cancel -> 409
{"error":"job already terminal"}

HTTP 409

## GET /stats
{"now_ms":1790177647512,"capacity":1,"in_use":0,"queued":0,"running":0,"submitted":1,"rejected":1,"completed":0,"deadline_missed":0,"timeout":0,"canceled":1,"failed":0,"met_deadline":0,"missed_deadline":0,"acquired_total":1,"released_total":1,"resource_conserved":true}
