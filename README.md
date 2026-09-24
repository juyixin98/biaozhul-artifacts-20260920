# 制品分阶段晋级服务(Artifact Staged Promotion)

纯后端的测试制品晋级状态机服务。使用 **Rust + Axum** 实现,数据保存在内存中。
三个阶段严格有序晋级:

```
dev  ──(verification 审批 + unit/integration 测试证明 passed)──▶  verification
verification ──(release 审批 + unit-test 与 release-gate 证明 passed)──▶ release

任意更早阶段 ◀── 回退(只追加历史,不删除/重建任何东西)
```

## 核心不变量

1. **同一不可变摘要绑定**:制品登记时绑定内容的 SHA-256 摘要,生命周期内不可改绑。
   内容按摘要寻址(content-addressed),重复上传相同字节幂等;没有任何接口可以
   “替换”某摘要下的内容或把已登记制品改绑到其他摘要。
2. **证明必须引用该摘要**:创建测试证明时必须提交摘要,服务端校验它与制品绑定摘要一致,
   否则返回 `409 DIGEST_MISMATCH`。审批同理,审批也绑定摘要与目标阶段。
3. **逐级晋级 + 门禁**:晋级只能到下一个阶段,且必须同时满足
   (a) 存在针对该摘要、目标阶段的 `approved` 审批;
   (b) 满足目标阶段的必要 `passed` 证明。
   - → `verification`:`unit-test` 或 `integration-test` 任一 passed;
   - → `release`:`unit-test` **与** `release-gate` 均 passed。
   `failed` 证明、`rejected` 审批都不算数。任何未通过必要证明的制品都不能发布。
4. **回退保留历史、不重建产物**:回退只往 `history` 追加一条 `rollback` 事件;
   内容字节、证明、审批全部保留。回退后门禁依然满足,可直接重新晋级,无需重新构建。
5. **并发安全**:所有写操作在同一把 `Mutex` 内完成“校验 + 变更”,
   并发回退恰好一个成功,其余得到 `409 INVALID_ROLLBACK_TARGET`。

## 目录结构

```
Cargo.toml          依赖清单(版本锁定,见 Cargo.lock)
src/model.rs        领域模型:Stage / Artifact / Proof / Approval / StageEvent
src/store.rs        内存存储 + 状态机(含 10 个单元测试)
src/api.rs          Axum 路由与处理函数
src/lib.rs          库入口(供集成测试)
src/main.rs         服务入口
tests/api_test.rs   HTTP 端到端集成测试(9 个,含并发回退)
examples/demo.sh    纯 curl 的接口演示脚本(即请求样例)
docs/demo-output.txt 最近一次真实运行 demo 的完整输出
```

## 依赖与运行环境

- Rust / Cargo(开发与实测版本:**1.98.1**,edition 2021)
- 主要依赖(版本在 `Cargo.lock` 锁定):
  - `axum` 0.7.9(HTTP 框架)
  - `tokio` 1.53.1(异步运行时,`full` feature)
  - `serde` 1.0.229 / `serde_json` 1.0.151
  - `sha2` 0.10.9(SHA-256)
  - `hex` 0.4.3
  - `tower` 0.4.13(仅测试用 `ServiceExt::oneshot`)
- 无数据库、无外部服务;无新增系统级依赖。`curl` 用于跑演示脚本。

## 启动命令

```bash
# 开发模式
cargo run

# 发布构建后运行(默认 0.0.0.0:8080,可用 PORT 覆盖)
cargo build --release
PORT=8080 ./target/release/artifact-promotion

# 离线环境(依赖已在本地缓存时)
cargo run --offline
```

健康检查:`GET http://127.0.0.1:8080/health`

## 测试

```bash
cargo test            # 10 个状态机单元测试 + 9 个 HTTP 集成测试
cargo clippy --all-targets
```

## HTTP 接口

错误统一返回:`4xx` + `{"error":{"code":"...","message":"..."}}`。

| 方法 | 路径 | 说明 | 成功状态 |
|---|---|---|---|
| PUT | `/artifacts/content` | 上传原始字节(请求体),服务端计算 SHA-256 并存入内容库;相同内容幂等 | 200 |
| GET | `/artifacts/content/:digest` | 按摘要取回原始字节(审计内容未被替换) | 200 |
| POST | `/artifacts` | 登记制品 `{id, digest}`,初始阶段 dev;同 id+同摘要幂等 | 201 / 200 |
| GET | `/artifacts` | 列出全部制品 | 200 |
| GET | `/artifacts/:id` | 制品全貌(含 history / proofs / approvals) | 200 |
| POST | `/artifacts/:id/proofs` | 添加测试证明(必须引用绑定摘要) | 201 |
| POST | `/artifacts/:id/approvals` | 添加审批(绑定摘要与目标阶段) | 201 |
| POST | `/artifacts/:id/promote` | 晋级到下一阶段 `{digest}` | 201 |
| POST | `/artifacts/:id/rollback` | 回退 `{digest,target_stage,reason?}` | 201 |
| GET | `/artifacts/:id/gates` | 门禁诊断视图(审批/证明满足情况) | 200 |

### 请求样例(完整流程见 `examples/demo.sh`)

```bash
BASE=http://127.0.0.1:8080

# 1. 上传内容(返回服务端计算的摘要)
printf 'release-binary-v1' | curl -sS -X PUT $BASE/artifacts/content \
  -H 'Content-Type: application/octet-stream' --data-binary @-
# -> {"digest":"ff443297...","size":17,...}
D=ff443297eb06cee14bc8acb5f9423e13e6c7fabd54bed21dd48fa6f4d465ddd3

# 2. 登记制品
curl -sS -X POST $BASE/artifacts -H 'Content-Type: application/json' \
  -d "{\"id\":\"app-1\",\"digest\":\"$D\"}"

# 3. verification 审批 + unit-test passed 证明,然后晋级
curl -sS -X POST $BASE/artifacts/app-1/approvals -H 'Content-Type: application/json' -d '{
  "approval_id":"ap-ver-1","digest":"'"$D"'","target_stage":"verification",
  "approver":"qa-lead","decision":"approved"}'
curl -sS -X POST $BASE/artifacts/app-1/proofs -H 'Content-Type: application/json' -d '{
  "proof_id":"p-unit","digest":"'"$D"'","kind":"unit-test","result":"passed"}'
curl -sS -X POST $BASE/artifacts/app-1/promote -H 'Content-Type: application/json' \
  -d "{\"digest\":\"$D\"}"

# 4. release 审批 + release-gate 证明,发布
curl -sS -X POST $BASE/artifacts/app-1/approvals -H 'Content-Type: application/json' -d '{
  "approval_id":"ap-rel-1","digest":"'"$D"'","target_stage":"release",
  "approver":"release-manager","decision":"approved"}'
curl -sS -X POST $BASE/artifacts/app-1/proofs -H 'Content-Type: application/json' -d '{
  "proof_id":"p-gate","digest":"'"$D"'","kind":"release-gate","result":"passed"}'
curl -sS -X POST $BASE/artifacts/app-1/promote -H 'Content-Type: application/json' \
  -d "{\"digest\":\"$D\"}"

# 5. 回退到 dev(历史保留,产物不动)
curl -sS -X POST $BASE/artifacts/app-1/rollback -H 'Content-Type: application/json' \
  -d "{\"digest\":\"$D\",\"target_stage\":\"dev\",\"reason\":\"incident #42\"}"
```

### 验收场景对应的拒绝行为

| 场景 | 结果 |
|---|---|
| 审批后用同一制品 id 绑定新内容摘要 | `409 IMMUTABLE_DIGEST` |
| 持新(不匹配的)摘要调用晋级/回退/加证明/加审批 | `409 DIGEST_MISMATCH` |
| 无审批晋级 | `409 APPROVAL_REQUIRED` |
| 审批通过但缺少必要 passed 证明 | `409 PROOF_REQUIRED`(响应中列出缺失类型) |
| 已在 release 再次晋级 | `409 ALREADY_AT_FINAL_STAGE` |
| 回退到当前阶段或更后阶段 | `409 INVALID_ROLLBACK_TARGET` |
| 16 个并发回退 | 1 × 201,15 × 409,历史仅 1 条 rollback |
| 登记引用内容库中不存在的摘要 | `400 INVALID_REQUEST` |

## 实测结果(2026-09-24,本机真实执行)

- `cargo build --offline` / `cargo build --release --offline`:成功。
- `cargo test`:**19 个测试全部通过**(10 单元 + 9 集成,0 失败)。
  集成测试包含:审批后替换内容被摘要绑定拦下、缺必要证明不能发布、
  重复晋级被拒、回退保留历史/证明/审批且内容字节不变、16 并发回退恰好一个成功。
- `cargo clippy --all-targets`:无警告。
- 真实启动 release 服务并运行 `examples/demo.sh`:全流程符合预期;
  完整输出保存在 `docs/demo-output.txt`(13 个 2xx,6 个预期内 409;
  并发回退 16 个请求返回 1×201 + 15×409;按旧摘要取回的字节仍为 `release-binary-v1`)。

## 未完成项 / 已知限制(如实说明)

- **纯内存存储**:重启后数据丢失;未做持久化与多实例一致性(题目未要求)。
- **无认证鉴权 / 多租户**:审批接口不校验调用者身份,`approver` 只是字段。
- **无删除接口**:制品与内容只能新增,符合“不可变”语义,但也意味着没有回收手段。
- **无分页/限流/请求体大小限制**:`GET /artifacts` 全量返回;上传体积只受内存约束。
- **时间戳精度到秒**(RFC3339 UTC,手写转换,未引入 chrono);并发事件可能同秒,
  以 `history` 数组顺序为准。
- **证明/审批模型为自声明式**:服务只校验摘要绑定与门禁组合,不验证证明来源真实性
  (例如签名、外部 CI 回调),生产场景需要。
- 未提供 Dockerfile / OpenAPI 描述文件;接口契约以本文档与 `demo.sh` 为准。
