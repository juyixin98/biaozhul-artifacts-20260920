#!/usr/bin/env bash
# End-to-end demo against a locally running GeoTerritory (docker-compose up).
# Requires: curl, jq.
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
KEY="${KEY:-demo-key-a}"
auth=(-H "X-API-Key: ${KEY}")
j=(-H "Content-Type: application/json")

echo "== health =="
curl -s "${BASE}/healthz"; echo

echo "== publish central-park-zone (priority 1) =="
curl -s "${j[@]}" "${auth[@]}" -X POST "${BASE}/v1/regions" -d '{
  "name":"central-park-zone","priority":1,
  "vertices":[
    {"lat":40.7650,"lng":-73.9857},
    {"lat":40.8005,"lng":-73.9580},
    {"lat":40.7965,"lng":-73.9490},
    {"lat":40.7640,"lng":-73.9730}]}' | jq .

sleep 1

echo "== batch import points =="
curl -s "${j[@]}" "${auth[@]}" -X POST "${BASE}/v1/points/batch" -d '{
  "points":[
    {"external_id":"p-inside-park","lat":40.7820,"lng":-73.9660},
    {"external_id":"p-boundary-edge","lat":40.7800,"lng":-73.9750},
    {"external_id":"p-unassigned","lat":40.9000,"lng":-74.2000}]}' | jq .

echo "== bbox query =="
curl -s "${auth[@]}" "${BASE}/v1/points/bbox?min_lat=40.70&max_lat=40.82&min_lng=-74.02&max_lng=-73.90" | jq .

echo "== nearest 5 points to 40.78,-73.97 =="
curl -s "${auth[@]}" "${BASE}/v1/points/nearest?lat=40.78&lng=-73.97&n=5" | jq .

echo "== invalid: antimeridian polygon (expect 400) =="
curl -s -o /dev/null -w "%{http_code}\n" "${j[@]}" "${auth[@]}" -X POST "${BASE}/v1/regions" -d '{
  "name":"bad-antimeridian","priority":2,
  "vertices":[{"lat":10,"lng":179},{"lat":20,"lng":179},{"lat":20,"lng":-179},{"lat":10,"lng":-179}]}'
