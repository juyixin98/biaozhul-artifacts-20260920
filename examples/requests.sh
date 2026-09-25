#!/usr/bin/env bash
# API 请求样例。先启动服务：python3 -m checkpoint_resume.server
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8377}"
CK="${CK:-/tmp/ckpt_demo_api}"

echo '== 1. 存活检查 =='
curl -s "$BASE/health"; echo

echo '== 2. 从头训练 10 步（新运行） =='
curl -s -X POST "$BASE/train" -H 'Content-Type: application/json' \
  -d "{\"config\": {\"checkpoint_dir\": \"$CK\", \"checkpoint_every\": 5}, \"steps\": 10, \"resume\": false}"; echo

echo '== 3. 断点续训 15 步（模拟进程重启后恢复） =='
curl -s -X POST "$BASE/train" -H 'Content-Type: application/json' \
  -d "{\"config\": {\"checkpoint_dir\": \"$CK\"}, \"steps\": 15, \"resume\": true}"; echo

echo '== 4. 当前训练器状态 =='
curl -s "$BASE/status"; echo

echo '== 5. 列出检查点及完整性 =='
curl -s "$BASE/checkpoints"; echo

echo '== 6. 校验最新检查点 =='
curl -s -X POST "$BASE/verify" -H 'Content-Type: application/json' \
  -d "{\"path\": \"$CK/step_00000025.ckpt.npz\"}"; echo

echo '== 7. 人为损坏后再校验（应返回 valid:false） =='
python3 - "$CK/step_00000025.ckpt.npz" <<'PY'
import sys
p = sys.argv[1]
d = bytearray(open(p, 'rb').read())
d[50] ^= 0xFF
open(p, 'wb').write(bytes(d))
PY
curl -s -X POST "$BASE/verify" -H 'Content-Type: application/json' \
  -d "{\"path\": \"$CK/step_00000025.ckpt.npz\"}"; echo

echo '== 8. 损坏后再次恢复（自动回退到次新有效检查点） =='
curl -s -X POST "$BASE/train" -H 'Content-Type: application/json' \
  -d "{\"config\": {\"checkpoint_dir\": \"$CK\"}, \"steps\": 3, \"resume\": true}"; echo
