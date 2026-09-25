#!/usr/bin/env bash
# Run every example request through the service and print verdicts.
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH=src

mkdir -p examples
python3 -m fixed_iir.cli synth --kind chirp -n 2048 --amplitude 0.8 \
    -o examples/in_chirp.pcm

for req in examples/request_*.json; do
    echo "=== $req"
    python3 -m fixed_iir.cli run "$req" -v
    echo "exit=$?"
done
