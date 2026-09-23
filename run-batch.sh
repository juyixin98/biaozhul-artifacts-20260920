#!/usr/bin/env bash
# 离线批处理：./run-batch.sh samples/request-basic.json
set -euo pipefail
cd "$(dirname "$0")"
if [[ $# -lt 1 ]]; then
  echo "用法: $0 <request.json>" >&2
  exit 2
fi
java -cp target/classes com.example.quantiles.service.Main batch "$1"
