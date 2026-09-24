#!/usr/bin/env bash
# End-to-end acceptance demo for the shard migration simulator.
# Usage: ./scripts/demo.sh [base_url]   (default http://localhost:8080)
set -euo pipefail
B="${1:-http://localhost:8080}"
j() { python3 -m json.tool; }
post() { curl -s -X POST "$B$1" -H 'Content-Type: application/json' ${2:+-d "$2"}; }
code() { curl -s -o /dev/null -w '%{http_code}' -X POST "$B$1" -H 'Content-Type: application/json' ${2:+-d "$2"}; }

echo "## 1) pre-migration writes (route v1, primary node-a)"
post /shards/orders/writes '{"payload":"order-1","client_route_ver":1}' | j
post /shards/orders/writes '{"payload":"order-2","client_route_ver":1}' | j

echo "## 2) START -> SNAPSHOT (control idempotency key k-start)"
post /shards/orders/migration/start '{"idempotency_key":"k-start"}' | j
echo "## duplicate START same key (replay=true)"
post /shards/orders/migration/start '{"idempotency_key":"k-start"}' | j
echo "## unkeyed duplicate START in wrong phase -> HTTP $(code /shards/orders/migration/start '{}')"

echo "## 3) write during SNAPSHOT (still node-a)"
post /shards/orders/writes '{"payload":"snap-write","client_route_ver":1}' | j

echo "## 4) target disconnect: snapshot-complete -> HTTP $(code /shards/orders/migration/snapshot-complete '{}')"
post /nodes/node-b/disconnect '{}' >/dev/null
echo "   snapshot-complete while down -> HTTP $(code /shards/orders/migration/snapshot-complete '{}')"
post /nodes/node-b/reconnect '{}' >/dev/null

echo "## 5) snapshot-complete -> CATCHUP (bulk + snapshot-phase writes)"
post /shards/orders/migration/snapshot-complete '{"idempotency_key":"k-snap"}' | j

echo "## 6) CATCHUP streaming; target down => confirmed on a, missing on b"
post /shards/orders/writes '{"payload":"catchup-ok","client_route_ver":1}' | j
post /nodes/node-b/disconnect '{}' >/dev/null
post /shards/orders/writes '{"payload":"catchup-missed","client_route_ver":1}' | j
echo "   switch while down    -> HTTP $(code /shards/orders/migration/switch '{}')"
post /nodes/node-b/reconnect '{}' >/dev/null
echo "   switch while lagging -> HTTP $(code /shards/orders/migration/switch '{}')"

echo "## 7) catchup then atomic SWITCH (and duplicate switch replay)"
post /shards/orders/migration/catchup '{"idempotency_key":"k-cu"}' | j
post /shards/orders/migration/switch '{"idempotency_key":"k-sw"}' | j
post /shards/orders/migration/switch '{"idempotency_key":"k-sw"}' | j

echo "## 8) stale route v1 after switch -> HTTP $(code /shards/orders/writes '{"payload":"stale","client_route_ver":1}')"
echo "## 9) old primary down; new primary node-b confirms v2"
post /nodes/node-a/disconnect '{}' >/dev/null
post /shards/orders/writes '{"payload":"after-switch-1","client_route_ver":2}' | j
post /nodes/node-a/reconnect '{}' >/dev/null
echo "## 10) old primary back but v1 still rejected -> HTTP $(code /shards/orders/writes '{"payload":"old-again","client_route_ver":1}')"
post /shards/orders/writes '{"payload":"after-switch-2","client_route_ver":2}' >/dev/null

echo "## 11) audit"
curl -s "$B/shards/orders/audit" | j
