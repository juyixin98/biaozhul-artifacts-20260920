# 请求样例（curl）
# 假定服务运行于 http://127.0.0.1:8080

# 1. 健康检查
curl -s http://127.0.0.1:8080/health

# 2. 查看当前策略（可信构建器、来源白名单、已注册公钥、本地材料）
curl -s http://127.0.0.1:8080/policy

# 3. 合法证明（HTTP 200，accepted=true）
curl -s -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' \
  -d @examples/request-valid.json

# 4. 输出被替换（HTTP 422，output_digest=OUTPUT_DIGEST_MISMATCH）
curl -s -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' \
  -d @examples/request-output-replaced.json

# 5. 材料缺失（HTTP 422，materials_complete=MATERIAL_MISSING）
curl -s -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' \
  -d @examples/request-missing-material.json

# 6. 跨构建器复用（HTTP 422，builder_binding=CROSS_BUILDER_REUSE）
curl -s -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' \
  -d @examples/request-cross-builder-reuse.json

# 7. 直接运行预置场景（等价于 3-6，无需手工构造请求）
curl -s -X POST http://127.0.0.1:8080/scenarios/01-valid
curl -s -X POST http://127.0.0.1:8080/scenarios/02-output-replaced
curl -s -X POST http://127.0.0.1:8080/scenarios/03-missing-material
curl -s -X POST http://127.0.0.1:8080/scenarios/04-cross-builder-reuse
