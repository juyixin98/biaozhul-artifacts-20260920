#!/usr/bin/env bash
# HTTP 验证入口请求样例。
# 前置：
#   cargo build --release
#   ./target/release/sst gen --n 5000 --seed 7 --prefix 757365723a \
#     | ./target/release/sst build --out /tmp/demo.sst --block-size 512 --restart-interval 8
#   ./target/release/sst serve --table /tmp/demo.sst --addr 127.0.0.1:18080 &
set -u
BASE=${BASE:-http://127.0.0.1:18080}

echo '== 存活探针 =='
curl -s "$BASE/health"; echo

echo '== 表统计 =='
curl -s "$BASE/stats"; echo

echo '== 点查：命中（demo.sst 的首个键） =='
curl -s "$BASE/get?key=757365723a00086fb5397148f0"; echo

echo '== 点查：未命中 =='
curl -s "$BASE/get?key=ff"; echo

echo '== 点查：空键（hex 空串） =='
curl -s "$BASE/get?key="; echo

echo '== 范围扫描 [757365723a, +∞)，限 2 条 =='
curl -s "$BASE/scan?start=757365723a&limit=2"; echo

echo '== 范围扫描 [k1, k2)，跨块 =='
curl -s "$BASE/scan?start=757365723a00&end=757365723a01"; echo

echo '== 严格校验 =='
curl -s "$BASE/verify"; echo

echo '== 错误处理：非法 hex（应 400） =='
curl -s -w '\nHTTP %{http_code}\n' "$BASE/get?key=zz"

echo '== 错误处理：未知路径（应 404） =='
curl -s -w '\nHTTP %{http_code}\n' "$BASE/nope"
