# Validate a project without persisting it.
curl -sS -X POST http://127.0.0.1:8080/v1/validate \
  -H 'Content-Type: application/json' \
  --data-binary @project.json
