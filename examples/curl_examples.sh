#!/usr/bin/env bash
# curl 请求样例（先启动服务：uvicorn main:app --port 8000）
set -e
BASE="${1:-http://127.0.0.1:8000}"

echo "== 健康检查 =="
curl -s "$BASE/health"; echo

echo "== 生成合成场景（5 相机 / 40 点 / 1px 噪声，含 2 个负深度点、2 个观测不足点）=="
curl -s -X POST "$BASE/v1/simulate" \
  -H 'Content-Type: application/json' \
  -d '{"n_cameras": 5, "n_points": 40, "noise_px": 1.0, "seed": 0,
       "n_behind_camera": 2, "n_under_observed": 2}' | head -c 600; echo " ..."

echo
echo "== 一键仿真 + Schur 补求解 =="
curl -s -X POST "$BASE/v1/solve_simulated" \
  -H 'Content-Type: application/json' \
  -d '{"scene": {"n_cameras": 5, "n_points": 40, "noise_px": 1.0, "seed": 0,
                  "n_behind_camera": 2, "n_under_observed": 2},
       "options": {"solver": "schur", "max_iterations": 50}}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps({k: d[k] for k in ('converged','iterations','final_cost','diagnostics')}, indent=2, ensure_ascii=False))"

echo "== 自定义问题求解（最小示例：2 相机 1 点）=="
curl -s -X POST "$BASE/v1/solve" \
  -H 'Content-Type: application/json' \
  -d '{
    "problem": {
      "intrinsics": {"fx": 500, "fy": 500, "cx": 320, "cy": 240},
      "cameras": [
        {"rvec": [0, 0, 0], "tvec": [0, 0, 0]},
        {"rvec": [0, 0.01, 0], "tvec": [-0.9, 0, 0]}
      ],
      "points": [[0.1, 0.0, 5.0]],
      "observations": [
        {"camera": 0, "point": 0, "uv": [330.0, 240.0]},
        {"camera": 1, "point": 0, "uv": [340.0, 240.0]}
      ]
    },
    "options": {"solver": "full", "max_iterations": 20}
  }' | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps({k: d[k] for k in ('converged','iterations','final_cost')}, indent=2, ensure_ascii=False))"
