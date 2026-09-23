#!/usr/bin/env bash
# 手工请求样例集合。先启动: ./run.sh 8080
# 然后执行: bash examples/requests.sh [baseUrl]
set -euo pipefail
B="${1:-http://localhost:8080}"
j() { python3 -m json.tool 2>/dev/null || cat; }

echo '### 健康检查'
curl -s "$B/health" | j; echo

echo '### 插入：嵌套区间'
curl -s -X POST "$B/intervals" -H 'Content-Type: application/json' -d '{"start":0,"end":100}' | j
curl -s -X POST "$B/intervals" -H 'Content-Type: application/json' -d '{"start":10,"end":90}'  | j
curl -s -X POST "$B/intervals" -H 'Content-Type: application/json' -d '{"start":40,"end":60}'  | j

echo '### 插入：相邻区间（端点相接但不重叠）'
curl -s -X POST "$B/intervals" -H 'Content-Type: application/json' -d '{"start":-10,"end":0}'  | j
curl -s -X POST "$B/intervals" -H 'Content-Type: application/json' -d '{"start":100,"end":110}' | j

echo '### 插入：重复区间（允许，分配不同 id）'
curl -s -X POST "$B/intervals" -H 'Content-Type: application/json' -d '{"start":40,"end":60}' | j

echo '### 交集查询：[45,46) 命中全部嵌套层 + 重复'
curl -s "$B/intervals?start=45&end=46" | j

echo '### 交集查询：[0,10) —— 与 [-10,0) 只是相邻，不相交'
curl -s "$B/intervals?start=0&end=10" | j

echo '### 指定时刻覆盖计数'
curl -s "$B/intervals/count?at=50"  | j   # 三层嵌套 + 重复 = 4
curl -s "$B/intervals/count?at=100" | j   # 100 是 [0,100) 的开端，又是 [100,110) 的闭端 = 1

echo '### 删除 id=2'
curl -s -X DELETE "$B/intervals/2" | j

echo '### 非法：空区间 / 逆序区间（均返回 400）'
curl -s -X POST "$B/intervals" -H 'Content-Type: application/json' -d '{"start":5,"end":5}' | j
curl -s -X POST "$B/intervals" -H 'Content-Type: application/json' -d '{"start":9,"end":3}' | j

echo '### 全量导出'
curl -s "$B/intervals/all" | j
