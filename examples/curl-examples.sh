#!/usr/bin/env bash
# curl 样例：用 printf 手工构造 RESP2 帧（bash 支持 \xNN 八进制/十六进制）。
# 前提：服务已启动（make run 或 go run .）。
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:7379}"

resp() {  # resp "CMD" "arg1" "arg2" ... -> RESP2 array frame on stdout
  printf '*'
  printf '%d' "$#"
  printf '\r\n'
  for a in "$@"; do
    printf '$%d\r\n%s\r\n' "${#a}" "$a"
  done
}

echo "== 1. PING 流水线（两条命令一次发送，回复按顺序返回）"
{ resp PING; resp PING hello; } |
  curl -s --data-binary @- -H 'Content-Type: application/octet-stream' \
       "$BASE/resp" | cat -v
echo

echo "== 2. SET/GET：空字符串与 NULL 的区别"
{ resp SET e ""; resp GET e; resp GET nope; } |
  curl -s --data-binary @- "$BASE/resp" | cat -v
echo
# 期望：+OK  /  $0（空 bulk）  /  $-1（NULL bulk）

echo "== 3. 二进制安全（0x00 / CR / LF / 0xFF）"
printf '*3\r\n$3\r\nSET\r\n$3\r\nbin\r\n$5\r\n' > /tmp/bin.resp
printf '\000\r\n\377\015' >> /tmp/bin.resp   # 5 payload bytes: NUL CR LF 0xFF CR
printf '\r\n' >> /tmp/bin.resp
printf '*2\r\n$3\r\nGET\r\n$3\r\nbin\r\n' >> /tmp/bin.resp
curl -s --data-binary @/tmp/bin.resp "$BASE/resp" | xxd
echo

echo "== 4. 事务（同一请求内流水线 MULTI/EXEC）"
{
  resp MULTI
  resp SET txk 1
  resp INCR txk
  resp INCR txk
  resp EXEC
} | curl -s --data-binary @- "$BASE/resp" | cat -v
echo

echo "== 5. 事务中语法错误 -> EXECABORT"
{
  resp MULTI
  resp SET a 1
  resp BOGUSCOMMAND x
  resp EXEC
} | curl -s --data-binary @- "$BASE/resp" | cat -v
echo

echo "== 6. 跨请求保持事务（X-Session 头）"
resp MULTI | curl -s -H 'X-Session: demo' --data-binary @- "$BASE/resp"
echo
resp SET k v | curl -s -H 'X-Session: demo' --data-binary @- "$BASE/resp"
echo
resp EXEC | curl -s -H 'X-Session: demo' --data-binary @- "$BASE/resp" | cat -v
echo

echo "== 7. JSON 便捷接口"
curl -s -H 'Content-Type: application/json' \
  -d '{"session":"j1","command":["MSET","j","42"]}' "$BASE/exec"
echo
curl -s -H 'Content-Type: application/json' \
  -d '{"session":"j1","command":["INCR","j"]}' "$BASE/exec"
echo
curl -s -H 'Content-Type: application/json' \
  -d '{"session":"j1","command":["GET","missing"]}' "$BASE/exec"
echo

echo "== 8. 半包演示：声明 100 字节 bulk 只发 3 字节 -> 400 + 协议错误帧"
printf '*1\r\n$100\r\nabc' | curl -s -w '\nHTTP %{http_code}\n' \
  --data-binary @- "$BASE/resp" | cat -v
