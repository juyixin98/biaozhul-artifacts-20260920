#!/usr/bin/env bash
# destroy.sh — tear down everything deploy.sh created.
set -euo pipefail
kind delete cluster --name quota-reservation
rm -rf "$(dirname "${BASH_SOURCE[0]}")/_output"
echo "[destroy] done"
