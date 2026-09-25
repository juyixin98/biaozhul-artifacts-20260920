#!/usr/bin/env bash
# dc-blocker HTTP 服务请求样例
# 先启动服务: PYTHONPATH=src python3 -c "from dc_blocker.service import serve; serve(port=8073)"
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8073}

# 1. 创建会话（采样率 48000 Hz，截止 5 Hz）
SID=$(curl -s -X POST "$BASE/sessions" \
  -d '{"sample_rate": 48000, "cutoff_hz": 5}' | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "session_id=$SID"

# 2. 分块流式处理（块大小任意，状态跨请求延续）
curl -s -X POST "$BASE/sessions/$SID/process" -d '{"samples": [0.3, 0.3, 0.3, 0.3, 0.3]}'; echo
curl -s -X POST "$BASE/sessions/$SID/process" -d '{"samples": [0.3, 0.3]}'; echo

# 3. 状态重置
curl -s -X POST "$BASE/sessions/$SID/reset" -d '{}'; echo

# 4. 重设采样率（截止频率不变，极点 R 重算，默认保留状态）
curl -s -X POST "$BASE/sessions/$SID/sample-rate" -d '{"sample_rate": 16000}'; echo

# 5. 删除会话
curl -s -X DELETE "$BASE/sessions/$SID"; echo
