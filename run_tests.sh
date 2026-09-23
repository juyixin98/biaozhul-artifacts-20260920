#!/usr/bin/env bash
# Build the simulator, run the Go test suite and execute every example
# scenario. The no-fence baseline is expected to FAIL (exit 1): it documents
# the corruption that fencing prevents. Everything else must pass.
set -u

cd "$(dirname "$0")"

if [ -x /usr/lib/go-1.22/bin/go ]; then
  export PATH=/usr/lib/go-1.22/bin:$PATH
fi

echo "== go vet =="
go vet ./... || exit 1

echo "== go test (race) =="
go test -race ./... || exit 1

echo "== build =="
go build -o bin/fls ./cmd/fls || exit 1

echo "== example scenarios =="
status=0
for d in renew-lost pause-expired late-message duplicate fuzz; do
  if ./bin/fls run "examples/$d.json" --quiet >/dev/null 2>err.txt; then
    echo "PASS  $d"
  else
    echo "FAIL  $d"; cat err.txt; status=1
  fi
done
rm -f err.txt

# Control experiment: no fence -> invariant violation expected.
if ./bin/fls run examples/no-fence.json --quiet >/dev/null 2>err.txt; then
  echo "FAIL  no-fence (expected exit 1, got 0)"; status=1
else
  echo "PASS  no-fence (correctly rejects with exit 1)"
fi
rm -f err.txt

echo "== built-in fuzz (10 seeds) =="
./bin/fls demo fuzz --quiet 2>/dev/null || { echo "FUZZ FAIL"; status=1; }

exit $status
