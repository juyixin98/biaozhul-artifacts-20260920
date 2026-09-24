#!/usr/bin/env bash
# 端到端接口演示：标定 -> 版本列表 -> 最新版本 -> 取内参（含跨尺寸被拒演示）-> 验签
# 用法：先启动服务（uvicorn app.main:app --port 8000），再 bash scripts/api_demo.sh
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8000}
CAM=cam_demo_01
GOOD=examples/good/1280x960

echo "== health =="
curl -s "$BASE/health"; echo

echo "== create calibration (18 good views) =="
ARGS=(-s -X POST "$BASE/api/v1/calibrations"
      -F camera_id="$CAM" -F pattern_cols=9 -F pattern_rows=6
      -F square_size_mm=25 -F min_views=5)
for f in "$GOOD"/*.jpg; do ARGS+=(-F "images=@$f"); done
RESP=$(curl "${ARGS[@]}")
VID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["version_id"])' <<<"$RESP")
echo "version_id=$VID"
python3 -c 'import json,sys; r=json.load(sys.stdin); print("accepted:",r["accepted_count"],"rms_px:",r["overall_rms_px"],"fx:",round(r["intrinsics"]["fx"],2))' <<<"$RESP"

echo "== list versions =="
curl -s "$BASE/api/v1/cameras/$CAM/versions" | python3 -m json.tool

echo "== intrinsics at bound resolution =="
curl -s "$BASE/api/v1/versions/$VID/intrinsics?width=1280&height=960" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print("fx:",r["intrinsics"]["fx"])'

echo "== cross-resolution reuse must be rejected (409) =="
curl -s -o /dev/null -w "http_status=%{http_code}\n" \
  "$BASE/api/v1/versions/$VID/intrinsics?width=1024&height=768"

echo "== verify HMAC signature =="
curl -s -X POST "$BASE/api/v1/versions/$VID/verify"; echo
