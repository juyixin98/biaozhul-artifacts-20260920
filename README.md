# CRD 迁移兼容验证（CRD Migration Compatibility Demo）

用 **Go + controller-runtime + envtest** 实现的纯后端示例，演示自定义资源
`Timer` 从 `v1alpha1`（整数秒）到 `v1`（结构化 Duration）的版本演进，并对
迁移过程中的兼容性问题给出**可验证**的答案：

- 旧版整数秒 ↔ 新版 `{seconds, nanos}` duration 的**转换 Webhook**（hub/spoke）；
- 新版独有字段 `spec.priority` 的**往返保存策略**（写入 annotation，旧客户端
  更新不会擦除它）；
- **不可表达值显式报错，绝不静默截断**（亚秒值降级即失败）；
- **未知字段策略由 schema 明示**：根级裁剪、`spec.extra` 保留
  （`x-kubernetes-preserve-unknown-fields`）；
- **存储版本迁移器**：逐对象重写、冲突重试、JSON 记录、失败可审计；
- **变更/校验 Webhook**：缺省字段、非法时长、枚举值；
- envtest 集成测试：真实 `kube-apiserver + etcd`、真实 RSA/x509 证书、真实
  TLS 调用 webhook。

> 本项目无任何前端页面。所有“计算、协议、密码”操作都真实执行；测试失败会
> 如实报告，不做模拟占位。

## 目录结构

```
api/v1alpha1/            旧版类型（intervalSeconds int64）
api/v1/                 新版类型（interval Duration + priority）
internal/conversion/    转换核心逻辑 + /convert HTTP webhook
internal/admission/     /mutate-v1-timer 与 /validate-v1-timer webhook
internal/validation/    两版本共享的校验规则
internal/migrator/      存储版本迁移器（含记录文件）
cmd/webhook/            webhook 服务进程
cmd/migrator/           迁移器 CLI
config/crd/bases/       CRD 定义（两版本 schema + 转换 webhook 接线）
config/webhook/         admission webhook 配置
config/manager/         Deployment/Service
fixtures/               两版本资源夹具 + 迁移记录夹具（成功/失败各一）
scripts/                envtest 下载、本地证书、迁移/回滚脚本
test/integration/       envtest 端到端测试（build tag: integration）
test/testenv/           envtest 启动 + CA/证书生成 + webhook 装配
docs/ROLLBACK.md        失败恢复手册
```

## 转换与兼容性语义

| 主题 | v1alpha1（旧） | v1（新，存储） | 策略 |
|---|---|---|---|
| 间隔/超时 | `intervalSeconds` 整数秒 | `interval{seconds,nanos}` | 整秒无损互转 |
| 亚秒精度 | **无法表达** | `nanos ∈ (-1e9,1e9)` | 降级时 webhook 报错，不截断 |
| 新增字段 | 不存在 | `priority: Low\|Normal\|High` | 存入 annotation `timer.example.com/v1-priority` 往返 |
| 自由数据 | `spec.extra` | `spec.extra` | 两版本 schema 均开启 preserve-unknown-fields |
| 根级未知字段 | — | — | `preserveUnknownFields:false`，apiserver 裁剪 |
| 缺省值 | — | 新建缺 `priority` | CRD schema `default: Normal` + mutating webhook |

Duration 遵循 `google.protobuf.Duration` 约定：`nanos` 与 `seconds` 同号，
`|nanos| < 1e9`。校验 webhook 对零时长、负时长、异号、越界 nanos、非法枚举
全部拒绝。

## 前置条件

- Go 1.22+
- `kubectl`、`openssl`（脚本使用）
- 可访问 Go 模块代理；envtest 控制面二进制约 160MB（首次测试时下载）

## 快速开始

```sh
# 1) 依赖
go mod download

# 2) 编译
make build

# 3) 纯单元测试（无需集群，秒级）
make test
```

### 启动 envtest 集成测试

```sh
# 方式 A：自动下载 kube-apiserver/etcd（GitHub release，约 160MB）
make envtest-download
export KUBEBUILDER_ASSETS=$HOME/.local/share/envtest-binaries/controller-tools/envtest
make integration-test

# 方式 B：若已安装 setup-envtest 且网络可达其默认源，make 会自动发现
make integration-test
```

## 本地对真实集群运行（可选）

```sh
# 1) 生成开发用 CA 与服务证书
scripts/gen-local-certs.sh /tmp/k8s-webhook-server/serving-certs

# 2) 安装 CRD 与 webhook 配置（需把 caBundle 替换为 scripts 输出的 ca.crt）
kubectl apply -f config/crd/bases/timer.example.com_timers.yaml
#   生产建议用 cert-manager 注入 caBundle；开发时见下方说明
kubectl apply -f config/manager/deployment.yaml
kubectl apply -f config/webhook/manifests.yaml

# 3) 启动 webhook
go run ./cmd/webhook \
  --tls-cert-file=/tmp/k8s-webhook-server/serving-certs/tls.crt \
  --tls-key-file=/tmp/k8s-webhook-server/serving-certs/tls.key

# 4) 应用夹具
kubectl apply -f fixtures/timers-v1alpha1.yaml
kubectl apply -f fixtures/timers-v1.yaml

# 5) 观察转换
kubectl get timers.v1alpha1.timer.example.com legacy-heartbeat -o yaml
kubectl get timers.v1.timer.example.com legacy-heartbeat -o yaml
```

> 开发环境若 webhook 与 apiserver 同机运行，也可以把 CRD 的
> `conversion.webhook.clientConfig` 改为 `url: https://<主机>:9443/convert`
> 并填入本地 CA（集成测试正是这样做的，见 `test/testenv`）。

## 存储版本迁移

```sh
# 查看当前 storedVersions
scripts/migrate-storage.sh status

# 升级：翻转 storage 标志 -> 迁移器逐对象重写 -> 生成记录
scripts/migrate-storage.sh up

# 回滚（带亚秒值预检；存在不可表达对象时直接拒绝并列出对象）
scripts/migrate-storage.sh down
```

迁移器直接调用 apiserver（不碰 etcd），因此转换失败会原样上浮为对象级错误。
记录文件默认写到 `./migration-records/`。失败处理见 **docs/ROLLBACK.md**。

仓库内附带两份**真实测试生成**的记录夹具：

- `fixtures/migration/migration-up-success.json` — 成功升级（2/2 migrated）；
- `fixtures/migration/migration-down-failed.json` — 含亚秒对象时降级失败，
  错误信息明确指出不可表达、拒绝隐式截断。

## 验收命令

```sh
# 一键完整验收（格式化、依赖、静态检查、单元测试、集成测试）
make verify

# 或分步
make tidy
make vet
make test
export KUBEBUILDER_ASSETS=$HOME/.local/share/envtest-binaries/controller-tools/envtest
make integration-test
```

集成测试覆盖以下需求点：

| 测试 | 验证内容 |
|---|---|
| `TestRoundTripThroughAPIServer` | 两版本读写往返、duration 与 extra 保真 |
| `TestDefaultedFields` | 缺省 priority/timeout 的默认行为 |
| `TestInvalidDurationsRejected` | 非法时长、非法枚举在准入时被拒 |
| `TestOldClientUpdatePreservesV1Fields` | **旧客户端更新不擦除新版 priority/extra** |
| `TestUnknownFieldPolicy` | 根级未知字段裁剪、`spec.extra` 未知键保留 |
| `TestSubsecondDowngradeFails` | 不可表达值显式报错 |
| `TestConversionTimeout` | 注入 3s 延迟，500ms 客户端超时按 deadline 失败 |
| `TestStorageMigrationAndFailureRecord` | 存储版本翻转、逐对象迁移、storedVersions、失败记录落盘 |

另外，`internal/...` 下有不依赖集群的单元测试，覆盖转换纯函数、handler
契约（含 context deadline）、校验规则与 admission 补丁。

## 设计说明：转换超时如何测试

真实 apiserver 的转换 webhook 是服务端调用，客户端无法直接“让 webhook
慢”。因此 webhook 识别一个测试/运维注解
`timer.example.com/inject-conversion-delay`（Go duration，如 `3s`），在处理
**该对象**前阻塞。测试以 v1alpha1 为存储版本、以 v1 请求（强制触发转换）：
耐心客户端（15s）成功，急躁客户端（500ms）在自己的 deadline 附近失败，
断言既检查错误也检查实际耗时上限。

## 依赖

依赖锁定在 `go.mod` / `go.sum`：

- `sigs.k8s.io/controller-runtime v0.19.4`
- `k8s.io/{api,apimachinery,client-go,apiextensions-apiserver} v0.31.4`
- envtest 控制面 `v1.31.0`（与 k8s.io/* 同 minor）

## 故障如实报告约定

迁移器在任何对象失败时退出码非 0 且记录 `failed>0`；测试对错误字符串做
断言而不是吞错；不可表达值不会被取整。这套行为在
`TestSubsecondDowngradeFails` 与失败记录夹具中可重复验证。
