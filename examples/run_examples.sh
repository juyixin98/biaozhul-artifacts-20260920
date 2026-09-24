#!/usr/bin/env bash
# 依次执行 examples/requests/ 下的样例请求,输出写入 examples/output/。
# 用法: bash examples/run_examples.sh   (在仓库根目录执行)
set -u
cd "$(dirname "$0")/.."
mkdir -p examples/output

run() {
    echo "== $* =="
    "$@"
    echo "(exit=$?)"
    echo
}

run python3 -m pcmval.cli request examples/requests/synthesize_fullscale24.json
run python3 -m pcmval.cli request examples/requests/validate_wav.json
run python3 -m pcmval.cli request examples/requests/validate_raw.json
run python3 -m pcmval.cli request examples/requests/roundtrip_sine16.json
run python3 -m pcmval.cli request examples/requests/batch.json
run python3 examples/make_fixtures.py
run python3 -m pcmval.cli request examples/requests/validate_fixtures.json
run python3 -m pcmval.cli synth --kind fullscale --bits 16 --sample-rate 8000 \
    --duration 0.002 -o examples/output/cli_fullscale16.wav
run python3 -m pcmval.cli validate examples/output/cli_fullscale16.wav
run python3 -m pcmval.cli roundtrip --kind fullscale --bits 24 --sample-rate 48000 \
    --duration 0.001 -o examples/output/cli_roundtrip24.wav
