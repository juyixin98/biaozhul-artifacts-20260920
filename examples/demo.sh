#!/usr/bin/env bash
# 端到端演示：需要先启动服务（cargo run），默认 localhost:3000。
# 用法：BASE=http://localhost:8080 ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:3000}"
C=(curl -sS)

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
req() { # method path [json body]
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    "${C[@]}" -X "$method" "$BASE$path" -H 'content-type: application/json' -d "$body"
  else
    "${C[@]}" -X "$method" "$BASE$path"
  fi
  echo
}

say "health"
req GET /health

say "begin A B C"
req POST /txn/A/begin
req POST /txn/B/begin
req POST /txn/C/begin

say "共享锁共存：A、B 先后取 r 上重叠区间的 shared"
req POST /txn/A/locks '{"resource":"r","mode":"shared","start":0,"end":10}'
req POST /txn/B/locks '{"resource":"r","mode":"shared","start":5,"end":15}'

say "排他等待：C 取 r 上 X，与 A/B 冲突 => queued"
req POST /txn/C/locks '{"resource":"r","mode":"exclusive","start":0,"end":10}'

say "清理现场：A/B/C 全部 abort"
req POST /txn/A/abort
req POST /txn/B/abort
req POST /txn/C/abort

say "二环死锁：A 持 r1，B 持 r2；B 等 r1；A 再请 r2 => 牺牲 B，A 获 r2"
req POST /txn/A/begin
req POST /txn/B/begin
req POST /txn/A/locks '{"resource":"r1","mode":"exclusive","start":0,"end":10}'
req POST /txn/B/locks '{"resource":"r2","mode":"exclusive","start":0,"end":10}'
req POST /txn/B/locks '{"resource":"r1","mode":"exclusive","start":0,"end":10}'
req POST /txn/A/locks '{"resource":"r2","mode":"exclusive","start":0,"end":10}'
req GET /waits
req POST /txn/A/commit

say "三环死锁：A->B->C->A => 牺牲 C；B 提交后 A 继续"
req POST /txn/A/begin
req POST /txn/B/begin
req POST /txn/C/begin
req POST /txn/A/locks '{"resource":"rA","mode":"exclusive","start":0,"end":10}'
req POST /txn/B/locks '{"resource":"rB","mode":"exclusive","start":0,"end":10}'
req POST /txn/C/locks '{"resource":"rC","mode":"exclusive","start":0,"end":10}'
req POST /txn/A/locks '{"resource":"rB","mode":"exclusive","start":0,"end":10}'
req POST /txn/B/locks '{"resource":"rC","mode":"exclusive","start":0,"end":10}'
req POST /txn/C/locks '{"resource":"rA","mode":"exclusive","start":0,"end":10}'
req GET /waits
req POST /txn/B/commit
req GET /waits
req POST /txn/A/commit

say "相邻不重叠区间：X/X/X 于 [0,10) [10,20) [20,30)，等待图必须为空"
for t in X Y Z; do req POST /txn/$t/begin >/dev/null; done
req POST /txn/X/locks '{"resource":"adj","mode":"exclusive","start":0,"end":10}'
req POST /txn/Y/locks '{"resource":"adj","mode":"exclusive","start":10,"end":20}'
req POST /txn/Z/locks '{"resource":"adj","mode":"exclusive","start":20,"end":30}'
req GET /waits
for t in X Y Z; do req POST /txn/$t/abort >/dev/null; done

say "锁升级：A、B 共享 r；A 升级 X 等待；B 升级 X => 死锁，B 牺牲，A 成 X"
req POST /txn/A/begin
req POST /txn/B/begin
req POST /txn/A/locks '{"resource":"up","mode":"shared","start":0,"end":10}'
req POST /txn/B/locks '{"resource":"up","mode":"shared","start":0,"end":10}'
req POST /txn/A/locks '{"resource":"up","mode":"exclusive","start":0,"end":10}'
req POST /txn/B/locks '{"resource":"up","mode":"exclusive","start":0,"end":10}'
req GET /resources
req POST /txn/A/commit

say "完成"
