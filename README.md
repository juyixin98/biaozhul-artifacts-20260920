# 资源配额预留器 (Resource Quota Reservation)

一个纯后端的 Kubernetes 资源预留系统：在真正创建 Pod **之前**，先从命名空间的
容量池（`ReservationPool`）中**预留** CPU 毫核和内存字节；预留带 TTL，过期自动
释放。使用 Go + controller-runtime + kind 实现，所有额度状态都持久化在 API
Server 的对象里（乐观并发），**绝不在进程内存中计数**。

---

## 1. 它解决什么问题

普通 `ResourceQuota` 只在对象**写入时**计数，无法表达"先占坑、稍后再起 Pod"。
本系统引入：

- **`ResourceClaim`**：一次资源申请（`cpu` / `memory` / `ttl`），有明确生命周期状态。
- **`ReservationPool`**：每个命名空间一个容量账本，其 `status.claims` 以
  `claim UID -> 贡献量` 持久化记录每一笔扣减。

申请 → 预留 → 绑定真实 Pod 的一致关系由两个调谐器和两个准入 Webhook 共同保证。

---

## 2. 状态机（过期释放与 Pod 创建竞争的核心）

```
                    ┌──────────────────────────────────────┐
                    │                                       │
 Pending ──CAS扣减成功──▶ Reserved ──发现带标签Pod(在TTL内)──▶ Bound
   │                     │  ▲                                │
   │容量不足/冲突耗尽      │  │ Pod消失且TTL未到                │ Pod存在
   ▼                     │  └────────────────────────────────┘ (真实对象
 Rejected                │  TTL到且确认无Pod                     始终优先)
                         ▼
                      Expired ──发现"迟到但已存在"的Pod──▶ Bound (late-pod 回补)
                         │
                         └─ 容量已退还；Webhook 拒绝之后再来的 Pod
```

**竞争处理的关键顺序**（见 `internal/controller/claim_controller.go` 的 `Reconcile`）：

1. **先找真实 Pod，再判断过期**。只要存在带 `quota.example.com/claim=<name>`
   标签的非终态 Pod，真实对象永远优先——即使 TTL 已过也不会释放（Bound 保留容量）。
2. **只有确认没有任何存活 Pod**，才允许 `Reserved -> Expired` 并退还容量。
3. 极端情况下 Pod 在"释放决策"与"准入传播"之间的窗口内出现：`Expired` 状态下
   发现真实 Pod 会走 late-pod 回补（重新扣减 → Bound），不留孤儿对象。
4. **准入 Webhook 是迟到 Pod 的确定性闸门**：claim 处于 `Expired/Rejected/
   Pending/已Bound` 时，或 Pod requests 超过预留时，直接拒绝 Pod 创建。

---

## 3. 如何防止超额与重复扣减（协议）

### 乐观并发（不是内存计数）

扣减发生在 `ReservationPool.status` 上。控制器：

1. `Get` 池对象（带 `resourceVersion`）；
2. 用纯函数 `ledger.EnsureDebited` 在内存里算出候选总额并做容量判断；
3. `Status().Update` 提交——API Server 校验 `resourceVersion`，若期间有别的
   写入者抢先提交，返回 **409 Conflict**，控制器重新 Get 并重试。

因此两个并发申请可能都"看到"够用，但只有一个能提交成功；另一个拿到 409，
重算后发现超额，被标记为 `Rejected`。串行化点是 **etcd + API Server**，控制器
进程重启、多副本都不会破坏不变量。重试有上限（默认 25，指数退避），耗尽则
`Rejected`，避免热点池把所有 claim 永久卡死。

### 幂等键 = claim UID

池账本是 `map[claimUID]贡献量`。同一 claim 重复调谐、控制器重启后重新 list
触发的再调谐，`EnsureDebited` 命中已有 UID 即 no-op，**绝不二次扣减**。
退还 `Credit` 同样按 UID 幂等。容量判断与"记录这笔"在同一次 status update 里，
不存在"判断通过但没记上"的中间持久态。

### Finalizer

claim 带 `quota.example.com/claim-finalizer`：删除时先退还容量再移除
finalizer，删除路径与过期路径共用同一个幂等 `creditPool`。

---

## 4. 真实的计算与密码学

- **单位换算**用 `k8s.io/apimachinery/pkg/api/resource.Quantity` 解析
  （与 Pod requests 同一套代码），CPU 统一为**整数毫核**、内存统一为**字节**，
  见 `internal/ledger/quantities.go`。Webhook 与控制器共享同一校验，不可能对
  单位产生分歧。
- **证书**：`hack/gen-certs.sh` 用 openssl 真实生成 4096 位自签 CA、2048 位
  服务证书（含 4 个 Service SAN），用 CA 签发并执行 `openssl verify` 验证链路，
  最后把 CA 以 base64 注入 `ValidatingWebhookConfiguration.caBundle`。不依赖
  cert-manager。

---

## 5. 目录结构

```
api/v1alpha1/              CRD 类型、状态机常量（+ controller-gen 生成 deepcopy/CRD）
config/crd/                生成的 CRD
config/webhook/            两个 ValidatingWebhookConfiguration（含 envtest 变体）
config/rbac/               Namespace/SA/Role/Binding/Service
config/manager/            controller Deployment
config/samples/            示例输入：池、正常/超额/过期/并发 claim、合法/超额 Pod
cmd/manager/               进程入口（装配 manager、webhook 路由）
internal/ledger/           数量解析 + 池账本纯函数（扣减/退还/容量判断）
internal/controller/       ResourceClaim 调谐器（状态机、CAS 重试、finalizer）
internal/webhook/          claim 校验（单位/上限/不可变）与 pod 校验
test/integration/          envtest 集成测试（真 apiserver + 真准入链）
hack/                      kind 配置、证书、部署、e2e 验收脚本
Dockerfile                 distroless 静态镜像（二进制在宿主机编译）
```

---

## 6. 本地启动

### 前置条件

Go 1.22.x、Docker、kind、kubectl、openssl、jq。本仓库在以下版本实测通过：
`go1.22.2`、`kind v0.23.0`、`kubectl v1.30.10`、`kindest/node:v1.30.10`、
`controller-gen v0.16.5`。

### 一键部署到 kind

```bash
make deploy          # = bash hack/deploy.sh
```

该脚本会：创建 kind 集群 → 宿主机静态编译 → distroless 打包 →
`docker save | ctr images import` 载入节点（本机 kind 0.23 对 containerd v2
的 `kind load` 探测有兼容问题，故用等价的 ctr 导入）→ openssl 签发证书 →
安装 CRD/RBAC/Webhook/Deployment，并等待 rollout 完成。

部署示例：

```bash
kubectl apply -f config/samples/quota-namespace.yaml   # namespace + 4C/4Gi 池
kubectl apply -f config/samples/resourceclaims.yaml    # 正常/超额/短TTL 申请
kubectl -n quota-demo get resourceclaims
kubectl -n quota-demo get reservationpool default-pool
```

### 验收（真实 kind 集群，22 个断言）

```bash
make e2e             # = bash hack/e2e-acceptance.sh
```

覆盖：预留入账、超额拒绝、Webhook 单位/上限拦截、spec 不可变、Pod 绑定、
超额 Pod 拒绝、**TTL 过期释放 + 迟到 Pod 拒绝**、**控制器滚动重启不重复扣减**、
**8 个并发申请恰好进 4 个、绝不超额**、finalizer 删除释放。

清理：

```bash
make teardown        # 删 kind 集群与生成证书
```

---

## 7. 自动化测试

### 单元测试（无需集群，秒级）

```bash
make test            # go test ./internal/...
```

- `internal/ledger`：CPU/内存解析与边界、扣减幂等、容量不足、退还幂等、并发模拟。
- `internal/controller`：用带 **409 Conflict 注入**的 client 直接调 `Reconcile`，
  验证乐观并发重试、重复调谐不二次扣减、超额拒绝、过期退还、late-pod 回补。

### 集成测试（envtest：真 kube-apiserver + etcd + 真准入链）

```bash
make envtest-assets  # 首次需要下载 kube-apiserver/etcd
make test-integration
```

12 个用例在真实 API Server 上运行控制器与两个 webhook：

| 用例 | 验证点 |
| --- | --- |
| TestBasicReservationShowsConsistentTriangle | claim↔pool↔Pod 三者一致，删 Pod 回 Reserved |
| TestConcurrentApplicationsNeverOvercommit | 10 个并发申请恰 6 预留 4 拒绝，池绝不超额 |
| TestControllerRestartIdempotency | 控制器崩溃重启后旧 claim 不重复扣减 |
| TestResourceVersionConflictUnderContention | 噪声 goroutine 持续 bump RV，CAS 重试收敛到精确总额 |
| TestExpiryReleasesCapacityAndRejectsLatePod | TTL 过期退还且 Webhook 拒绝迟到 Pod |
| TestExpiryVsRealObjectStateTransition | TTL 到期但 Pod 存在则保持 Bound；删除 webhook 模拟释放窗口 Pod，验证 Expired→Bound 回补 |
| TestDeletionReleasesCapacity | finalizer 退还后对象真正消失 |
| TestClaimPoolAndLedgerSumsAgree | claim 预留之和 == 池总额 |
| TestClaimWebhookValidatesUnitsAndBounds | 错误单位/超上限在准入期被拒 |
| TestClaimSpecImmutable | spec 创建后不可变 |
| TestPodWebhookGuardsReservation | Pending/Bound/Expired/超额/无 label 各路径 |
| TestExpiredThenClaimReuse | 过期释放的容量可被新申请复用 |

> 单元测试 + 集成测试 + kind e2e 三层全部在本环境真实执行通过（见下方"实测记录"）。

---

## 8. 关键设计说明与边界

- **单一池/命名空间**：池对象固定名 `default-pool`，把乐观并发的争用域限制在
  命名空间内，与内置 `ResourceQuota` 的作用域一致。
- **spec 不可变**：预留是与池的扣减契约，改 spec 需要退/重扣的复杂协议；
  需要变更时创建新 claim。
- **一个 claim 一个 Pod**：Bound 后第二个带相同标签的 Pod 被拒绝。
- **终态 Pod 不占位**：`Succeeded/Failed` 的 Pod 视为不再占用，TTL 内可重新绑定。
- **webhook `failurePolicy: Fail` + CEL matchCondition**：只有带 claim 标签的
  Pod 才被拦截，其余 Pod 完全不受影响。
- 延迟来源：未绑定 claim 用 `RequeueAfter(ttl)` 精确定时；Pod/池事件通过
  mapper 入队，删除与过期都能及时反映。

---

## 9. 实测记录

以下均在交付环境真实执行（非模拟描述）：

- `go test ./internal/...`：`ok`（ledger + controller 全部通过）。
- `go test ./test/integration/...`：**12/12 PASS**（envtest 真 apiserver）。
- `bash hack/e2e-acceptance.sh`（kind v1.30.10）：**22 passed, 0 failed**。
- `openssl verify`：服务证书链到自签 CA，校验 OK。

如任一命令失败，会以非零退出码和具体断言输出如实报告，不会静默忽略。
