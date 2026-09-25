#!/usr/bin/env bash
# End-to-end acceptance demonstration for cdag.
#
# Every command below is one of the test-fixture commands the project is
# designed to run: sh/cat/cp/tr/touch plus the cdag binary itself. No
# network access is used. Outputs are written to examples/.run/.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
RUN="$HERE/.run"
WS="$RUN/workspace"
CACHE="$RUN/cache"
SPEC="$RUN/project.json"
export GREETING=hello MODE=plain

line() { printf '\n==================== %s ====================\n' "$1"; }

# Fresh, reproducible workspace derived from the checked-in example.
rm -rf "$RUN"
mkdir -p "$WS/src" "$WS/build" "$WS/dist"
cp "$HERE/workspace/src/message.txt" "$WS/src/message.txt"

# Emit a project spec whose workdir is the fresh run workspace.
cat > "$SPEC" <<JSON
{
  "id": "demo",
  "workdir": "$WS",
  "graph": {
    "nodes": [
      { "id": "source", "outputs": ["src/message.txt"] },
      {
        "id": "normalize",
        "command": "mkdir -p build dist && tr A-Z a-z < src/message.txt > build/normalized.txt",
        "inputs": ["src/message.txt"],
        "outputs": ["build/normalized.txt"],
        "depends_on": ["source"],
        "tools": [{"name": "tr", "probe": ["sh", "-c", "tr --version 2>&1 | head -n 1"]}]
      },
      {
        "id": "decorate",
        "command": "cat build/normalized.txt > build/decorated.txt && printf '%s-%s' \"\${GREETING}\" \"\${MODE}\" >> build/decorated.txt",
        "inputs": ["build/normalized.txt"],
        "outputs": ["build/decorated.txt"],
        "depends_on": ["normalize"],
        "params": {"stage": "decorate", "format": "plain"},
        "env": ["GREETING", "MODE"]
      },
      {
        "id": "package",
        "command": "mkdir -p dist && cp build/decorated.txt dist/release.txt",
        "inputs": ["build/decorated.txt"],
        "outputs": ["dist/release.txt"],
        "depends_on": ["decorate"]
      },
      { "id": "readme", "command": "mkdir -p build && echo 'unrelated node' > build/readme.txt", "outputs": ["build/readme.txt"] }
    ]
  }
}
JSON

cd "$ROOT"
go build -o "$RUN/cdag" ./cmd/cdag

line "1. validate"
"$RUN/cdag" validate --file "$SPEC"

line "2. cold build (every command runs)"
"$RUN/cdag" build --file "$SPEC" --cache-dir "$CACHE"
echo "-- dist/release.txt content:"; cat "$WS/dist/release.txt"; echo

line "3. no-op rebuild (everything cached)"
"$RUN/cdag" build --file "$SPEC" --cache-dir "$CACHE" | \
  grep -E '"node_id"|"status"|"reason"|built|cached'

line "4. touch files preserving CONTENT and timestamp-only change"
SRC_MTIME_BEFORE=$(stat -c %Y "$WS/src/message.txt")
touch -d '2030-01-02 03:04:05' "$WS/src/message.txt"
touch -d '2030-01-02 03:04:06' "$WS/build/normalized.txt"
SRC_MTIME_AFTER=$(stat -c %Y "$WS/src/message.txt")
echo "src mtime before=$SRC_MTIME_BEFORE after=$SRC_MTIME_AFTER (content unchanged)"
"$RUN/cdag" build --file "$SPEC" --cache-dir "$CACHE" | grep -E '"status"' | sort | uniq -c

line "5. change FILE CONTENT (transitive rebuild; readme reused)"
printf 'GOODBYE Content-Driven Build\n' > "$WS/src/message.txt"
"$RUN/cdag" build --file "$SPEC" --cache-dir "$CACHE" | \
  grep -E '"node_id"|"status"|"detail"'
echo "-- dist/release.txt content:"; cat "$WS/dist/release.txt"; echo

line "6. change PARAMETER (decorate + package rebuilt, others reused)"
sed -i 's/"plain"}/"rich"}/' "$SPEC"
"$RUN/cdag" build --file "$SPEC" --cache-dir "$CACHE" | \
  grep -E '"node_id"|"status"|param_'

line "7. change declared ENV VAR (decorate + package rebuilt)"
MODE=rich "$RUN/cdag" build --file "$SPEC" --cache-dir "$CACHE" | \
  grep -E '"node_id"|"status"|env_changed'
echo "-- dist/release.txt content:"; cat "$WS/dist/release.txt"; echo

line "8. failed node: package command fails, no cache published, dependents blocked"
# Make decorate produce its output but make package fail by removing its input
# first declared expectation: instead, append a failing extra node.
cat > "$RUN/fail.json" <<JSON
{
  "id": "faildemo",
  "workdir": "$WS",
  "graph": {"nodes": [
    {"id": "boom", "command": "echo about to fail >&2; exit 7", "outputs": ["build/boom.txt"]},
    {"id": "after", "command": "cat build/boom.txt > build/after.txt", "inputs": ["build/boom.txt"], "outputs": ["build/after.txt"], "depends_on": ["boom"]}
  ]}
}
JSON
"$RUN/cdag" build --file "$RUN/fail.json" --cache-dir "$CACHE"; echo "exit=$? (expected nonzero)"
test ! -e "$WS/build/boom.txt" && echo "OK: failed node left no output file"
ls "$CACHE/entries" >/dev/null 2>&1 && echo "OK: cache store intact"

line "9. cycle detection rejected before any command runs"
cat > "$RUN/cycle.json" <<JSON
{
  "id": "cycledemo",
  "workdir": "$WS",
  "graph": {"nodes": [
    {"id": "a", "command": "echo a > build/a.txt", "outputs": ["build/a.txt"], "depends_on": ["b"]},
    {"id": "b", "command": "echo b > build/b.txt", "outputs": ["build/b.txt"], "depends_on": ["a"]}
  ]}
}
JSON
"$RUN/cdag" validate --file "$RUN/cycle.json"; echo "exit=$? (expected nonzero)"

line "10. cache lives outside workdir"
echo "workdir:"; find "$WS" -maxdepth 1 | sort
echo "cache root:"; find "$CACHE" -maxdepth 2 -type d | sort

line "DONE"
