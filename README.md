# CRD 迁移兼容验证 (CRD migration compatibility verification)

纯后端项目：用 Go + controller-runtime + envtest 实现一个示例 CRD（`Task`）从
**v1alpha1** 到 **v1** 的转换 Webhook，并对迁移兼容性做端到端验证。无前端。

* 旧版用**整数秒**（`spec.timeoutSeconds`）表示超时；
* 新版用结构化 **Duration**（`spec.timeout.{seconds,nanos}`），并新增 `priority`、`tags` 字段；
* 不可表达的值（亚秒精度）**明确报错，绝不静默截断**；
* 新增字段通过**保留注解往返保存**，旧客户端 GET+PUT 不会擦除新版信息；
* 未知字段是否保留由 **CRD schema 显式声明**；
* 提供存储版本迁移脚本（真实 GET/UPDATE 重写）与**失败恢复记录**（JSONL）。

## 为什么需要它

Kubernetes 升级 CRD 存储版本后，旧客户端仍然存在。风险有三类，本项目逐一用真实
apiserver 验证：

1. **数据丢失**：旧客户端读旧版本、改一个字段、写回，把新字段抹掉。
2. **静默截断**：新版 2.5s 在旧版只有整数秒，若被悄悄截成 2s，行为会错。
3. **存储不一致**：切换 storage version 后，etcd 里仍是旧版本字节，需要重写迁移。

## 架构

```
                          ┌──────────────────────────────────────────────┐
  旧客户端 (v1alpha1) ──▶ │ kube-apiserver (envtest: 真实 apiserver+etcd)  │
  新客户端 (v1)        ──▶ │                                                │
                          │  admission: mutating/validating webhook        │
                          │  storage:   v1                                  │
                          │  serving:   v1 + v1alpha1 (转换 webhook)        │
                          └───────────────┬──────────────────────────────┘
                                          │ HTTPS /convert /mutate-* /validate-*
                                          ▼
                          ┌──────────────────────────────────────────────┐
                          │ webhook-server (controller-runtime TLS)        │
                          │  internal/conversion  非结构化 JSON map 转换    │
                          │  internal/webhook     ConversionReview/Admission│
                          └──────────────────────────────────────────────┘
                                          ▲
                          cmd/storage-migrate：list→update 重写存储 + JSONL 记录
```

关键设计在 [`docs/roundtrip-strategy.md`](docs/roundtrip-strategy.md)，要点：

* 转换在 **unstructured JSON map** 上进行，不经过会裁剪未知键的强类型 Go 结构；
  `priority/tags` 在 v1→v1alpha1 时收进注解 `migration.example.io/v1-spec-preserve`，
  回升时还原并删除注解；
* `timeout.nanos != 0` 无法用整数秒表达 → 返回 `InexpressibleValue`；
* CRD `spec.preserveUnknownFields: false`，但在两个版本的 `spec`/`status` 上显式
  `x-kubernetes-preserve-unknown-fields: true`；
* 每个 admission webhook 都设 `matchPolicy: Exact`，避免 v1 写操作被等价匹配到
  v1alpha1 webhook 而触发有损转换。

## 目录

```
api/v1alpha1, api/v1            两版 Go 类型（手写 DeepCopy）
internal/conversion             核心转换逻辑（可独立单测，不依赖 apiserver）
internal/webhook                /convert 与 mutating/validating HTTP 处理器
cmd/webhook-server              生产形态的 TLS webhook 进程
cmd/storage-migrate             存储版本迁移 CLI（重写 + JSONL 记录 + storedVersions 裁剪）
cmd/dev-env                     一条命令拉起本地完整环境（envtest+CRD+webhook）
config/crd                      CRD（双版本 schema、conversion webhook、未知字段策略）
config/webhook                  MWC/VWC（matchPolicy: Exact）
test/fixtures                   两版资源夹具 + 非法时长夹具 + 旧客户端 PUT 示例
test/failure-records            真实生成的迁移/失败恢复记录及 runbook
test/integration               envtest 端到端测试
docs/roundtrip-strategy.md      字段往返与数据保留契约
hack/acceptance.sh              自动化验收脚本
```

## 前置要求

* Go 1.22+
* `setup-envtest`（`make install-tools` 会装），用于下载 kube-apiserver/etcd 二进制
* `kubectl`

## 本地启动

```bash
make install-tools          # 一次性：安装 setup-envtest
make dev                    # 起 envtest 控制面 + CRD + 全部 webhook，前台运行
```

就绪后另开终端，按打印的提示操作（kubeconfig 写到 `/tmp/crd-migrate-dev-kubeconfig`）：

```bash
export KUBECONFIG=/tmp/crd-migrate-dev-kubeconfig
kubectl apply -f test/fixtures/task-v1alpha1.yaml
kubectl get tasks.v1alpha1.migration.example.io nightly-backup   # timeoutSeconds: 120
kubectl get tasks.v1.migration.example.io nightly-backup         # timeout.seconds: 120
kubectl apply -f test/fixtures/task-v1.yaml
kubectl get tasks.v1alpha1.migration.example.io latency-sensitive
#   → InexpressibleValue: ... sub-second component 500000000ns ...（不截断）
```

生产形态的独立 webhook 进程（自带证书目录，对接 kind/k3d 等真实集群）：

```bash
go run ./cmd/webhook-server --port 9443 --cert-dir /path/to/tls --conversion-timeout 10s
```

## 存储版本迁移

```bash
# 预览（只读+转换，不 update）
go run ./cmd/storage-migrate --dry-run --record-file records.jsonl
# 正式迁移：逐对象 GET(服务端转换) → UPDATE(storage 重写为 v1)
go run ./cmd/storage-migrate --record-file records.jsonl
# 全成功后自动把 CRD status.storedVersions 裁剪为 [v1]（--trim-stored-versions=false 可关）
```

失败不静默：非零退出，失败对象写入 JSONL；重跑同一命令即可恢复（已迁移对象幂等
跳过）。真实示例与恢复流程见 [`test/failure-records/README.md`](test/failure-records/README.md)。

## 测试与验收

```bash
make unit-test        # 转换逻辑 + webhook 处理器（秒级，无需集群）
make integration-test # envtest 端到端（真实 apiserver+etcd+TLS webhook）
make test             # 上面两者
make slow-test        # 额外的转换超时 e2e（真实 apiserver 30s 硬超时，约 31s）
make acceptance       # 拉起 dev-env，跑 11 条 kubectl 级断言后自动清理
make lint             # go vet + gofmt
```

覆盖的验收点（对应需求）：

| 需求 | 测试 |
|------|------|
| 读写往返 | `TestRoundTripWholeSeconds`、`TestLegacyCreateDefaultsAndReadsAsV1` |
| 缺省字段 | `TestMutateV1Defaults`、`TestV1CreateDefaultsAndValidates` |
| 非法时长明确报错 | `TestInvalidDurationsRejected`、`TestValidateV1RejectsBadDuration`、夹具 `invalid-duration-v1.yaml` |
| 不可表达值不截断 | `TestSubSecondNanosRejected`、`TestSubSecondValueFailsServingToOldClient` |
| 转换超时 | `TestTimeoutHonoursContext`、`TestConversionHandlerTimeout`、`TestConversionTimeoutEndToEnd`（RUN_SLOW_TESTS=1） |
| 旧客户端更新不擦除新版信息 | `TestNewFieldsSurviveOldClient`、`TestOldClientUpdateDoesNotEraseV1Fields` |
| 未知字段按 schema 保留 | `TestUnknownFieldsPassThrough`、`TestUnknownFieldsSurviveConversion` |
| 存储迁移 + 失败恢复记录 | `TestStorageMigrateCLI`（注入崩溃→重跑）、`TestStorageMigrateDryRun`、`test/failure-records/*` |

最近一次 `make acceptance` 实测结果：**11 passed, 0 failed**。

## 依赖

依赖已在 `go.mod` / `go.sum` 锁定，主要版本：

* `sigs.k8s.io/controller-runtime` v0.19.0
* `k8s.io/{api,apimachinery,apiextensions-apiserver,client-go}` v0.31.0
* envtest 控制面二进制 Kubernetes v1.31.0（由 setup-envtest 管理，不进 go.mod）

## 说明：真实执行

所有计算、网络协议（ConversionReview / AdmissionReview / RFC6902 patch）与测试中的
TLS、kube-apiserver、etcd 均为真实执行，无 mock 替身。任何一步失败都会在对应测试或
CLI 退出码中如实暴露。
