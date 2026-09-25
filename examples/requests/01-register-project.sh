# Register a project (same JSON schema as the CLI project file, absolute
# workdir recommended when talking to a long-running server).
curl -sS -X POST http://127.0.0.1:8080/v1/projects \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "demo",
    "workdir": "/abs/path/to/examples/workspace",
    "graph": {
      "nodes": [
        { "id": "source", "outputs": ["src/message.txt"] },
        {
          "id": "normalize",
          "command": "mkdir -p build dist && tr A-Z a-z < src/message.txt > build/normalized.txt",
          "inputs": ["src/message.txt"],
          "outputs": ["build/normalized.txt"],
          "depends_on": ["source"],
          "tools": [{ "name": "tr", "probe": ["sh", "-c", "tr --version 2>&1 | head -n 1"] }]
        },
        {
          "id": "decorate",
          "command": "cat build/normalized.txt > build/decorated.txt && printf \"%s-%s\" \"${GREETING}\" \"${MODE}\" >> build/decorated.txt",
          "inputs": ["build/normalized.txt"],
          "outputs": ["build/decorated.txt"],
          "depends_on": ["normalize"],
          "params": {"stage": "decorate", "format": "plain"},
          "env": ["GREETING", "MODE"]
        },
        {
          "id": "package",
          "command": "cp build/decorated.txt dist/release.txt",
          "inputs": ["build/decorated.txt"],
          "outputs": ["dist/release.txt"],
          "depends_on": ["decorate"]
        },
        {
          "id": "readme",
          "command": "mkdir -p build && echo unrelated > build/readme.txt",
          "outputs": ["build/readme.txt"]
        }
      ]
    }
  }'
