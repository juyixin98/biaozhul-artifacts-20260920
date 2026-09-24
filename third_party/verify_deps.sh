#!/usr/bin/env bash
# 重新计算并核对 third_party 下所有锁定依赖的 SHA256。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

expected=(
"8586084f71f9bde545ee7fa6d00288b264a2b7ac3607b974e54d13e7162c1c72  eigen-3.4.0.tar.gz"
"9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6  json.hpp"
)

fail=0
for line in "${expected[@]}"; do
  want="${line%% *}"
  file="${line##* }"
  if [[ ! -f "$file" ]]; then
    echo "MISSING  $file"; fail=1; continue
  fi
  got="$(sha256sum "$file" | awk '{print $1}')"
  if [[ "$got" == "$want" ]]; then
    echo "OK       $file  $got"
  else
    echo "HASH BAD $file"
    echo "  expected $want"
    echo "  got      $got"
    fail=1
  fi
done

if [[ ! -d eigen-3.4.0/Eigen ]]; then
  echo "需要解压 Eigen: tar xzf eigen-3.4.0.tar.gz"
  fail=1
fi

if (( fail )); then
  echo "依赖校验失败" >&2
  exit 1
fi
echo "全部依赖校验通过"
