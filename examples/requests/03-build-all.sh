# Build the whole graph.
curl -sS -X POST http://127.0.0.1:8080/v1/builds \
  -H 'Content-Type: application/json' \
  -d '{"project_id": "demo"}'
