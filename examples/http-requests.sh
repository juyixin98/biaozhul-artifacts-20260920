#!/usr/bin/env bash
# extsort HTTP 验证入口 —— 请求样例
#
# 用法：
#   1) 先启动服务：
#        ./target/release/extsort serve --root ./httproot --addr 127.0.0.1:8080
#   2) 另开终端运行本脚本：
#        bash examples/http-requests.sh
#
# 需要 curl。脚本只依赖 /healthz、/jobs 等接口，无前端。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say() { printf '\n==== %s ====\n' "$*"; }

say "1) 健康检查"
curl -sS "$BASE/healthz"; echo

say "2) 整行升序（请求体即原始数据）"
printf 'banana\napple\ncherry\napple\n' > "$TMP/lines.txt"
curl -sS -X POST "$BASE/jobs?budget=163840&lanes=2" \
  --data-binary @"$TMP/lines.txt"; echo

say "3) 复合键：第1列升序、第2列降序（极小预算，强制落盘）"
cat > "$TMP/rows.csv" <<'CSV'
2,b,0
1,y,1
2,a,2
1,x,3
3,z,4
1,y,5
CSV
RESP="$(curl -sS -X POST \
  "$BASE/jobs?spec=1:asc,2:desc&delim=,&budget=163840&lanes=2" \
  --data-binary @"$TMP/rows.csv")"
echo "$RESP"
JOB="$(printf '%s' "$RESP" | sed -n 's/.*"job_id":"\([^"]*\)".*/\1/p')"
echo "job id = $JOB"

say "4) 查看作业状态 JSON"
curl -sS "$BASE/jobs/$JOB"; echo

say "5) 拉取排序结果（重复键按输入序号稳定：两条 1,y 保持 1 在 5 前）"
curl -sS "$BASE/jobs/$JOB/output"

say "6) 列出全部作业"
curl -sS "$BASE/jobs"; echo

say "7) 故障注入：归并输出创建失败（返回 500 + fault_injected；作业停留在 merge，可恢复）"
# 造一份足够大、必然进入归并阶段的数据
python3 - "$TMP/big.csv" <<'PY'
import sys, random
random.seed(1)
with open(sys.argv[1], "w") as f:
    for i in range(20000):
        g = random.randrange(30)
        n = "".join(random.choice("abcdefg") for _ in range(random.randint(3, 12)))
        f.write(f"{g:02},{n},{i}\n")
PY
curl -sS -o "$TMP/fault.json" -w "HTTP %{http_code}（预期 500）\n" -X POST \
  "$BASE/jobs?spec=1:asc,2:asc&budget=163840&lanes=2" \
  -H 'X-Fault-Inject: create_fail:m-1-' \
  --data-binary @"$TMP/big.csv"
FJOB="$(sed -n 's/.*"job_id":"\([^"]*\)".*/\1/p' "$TMP/fault.json" | head -1)"
cat "$TMP/fault.json"; echo

say "7b) 从已提交分段恢复该故障作业"
curl -sS -X POST "$BASE/jobs/$FJOB/resume"; echo

say "7c) 恢复结果应为完整 20000 行"
curl -sS "$BASE/jobs/$FJOB/output" | wc -l

say "8) 删除作业"
curl -sS -X DELETE "$BASE/jobs/$JOB"; echo
curl -sS -X DELETE "$BASE/jobs/$FJOB"; echo

echo
echo "样例完成。"
