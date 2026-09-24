# quota-reserver — 资源配额预留器

在测试命名空间内提供「先申请、后建 Pod」的资源配额预留能力。纯后端，基于
Go + controller-runtime，可在 kind 上一键拉起。

## 对象模型

```
QuotaPool (CRD, 命名空间级)                ResourceRequest (CRD, 命名空间级)
┌──────────────────────────────┐          ┌──────────────────────────────┐
│ spec:                        │  预留扣减  │ spec:                        │
│   cpuMilli    (总容量, 毫核)  │◄─────────│   pool        (目标池名)      │
│   memoryBytes (总容量, 字节)  │  乐观并发 │   cpuMilli    (申请, 毫核)    │
│ status:                      │  UID 账本 │   memoryBytes (申请, 字节)    │
│   allocations[] (UID→量)     │          │   ttlSeconds  (预留存活期)    │
│   usedCPUMilli / usedMemory  │          │ status: phase / expiresAt /   │
└──────────────────────────────┘          │   boundPod / reason / message │
                                          └──────────────┬───────────────┘
                                                         │ label 绑定 + 准入校验
                                                         ▼
                                                   Pod (真实对象)
                                            labels: quota.biaozhu.dev/request=<名字>
                                            resources.requests 必须等于预留量
```

一致性不变式（测试与验收脚本均会断言）：

```
pool.status.used* == sum(pool.status.allocations[*])
                    == sum(spec of 处于 Reserved/Bound 的 ResourceRequest)
                    == Bound 请求对应真实 Pod 的 resources.requests
```

## 状态机

所有状态转移都是显式的，且由 apiserver 的 resourceVersion 乐观并发保护：

```
                 容量足够                    Pod 准入(webhook 原子转移)
Pending ───────────────────► Reserved ──────────────────────► Bound ──Pod 结束/被删──► Released
   │                           │                                                        (终态)
   │ 容量不足                   │ TTL 到期(释放配额)
   └─────────────► Rejected    └─────────────► Expired ──► 迟到 Pod 被 webhook 拒绝,
                  (终态)                        (终态)     漏网者由控制器清扫删除
```

关键设计：

- **乐观并发防超额**：扣减先改 `QuotaPool.status` 的 UID 账本再 `Status().Update`，
  冲突自动重试（`retry.RetryOnConflict`）。并发申请由 apiserver 的
  resourceVersion 仲裁，绝不超额。
- **同请求重复调谐不重复扣减**：账本以 `ResourceRequest.UID` 为键，重复
  Reserve 是幂等空操作；即使控制器在「改完池子、没来得及改状态」之间崩溃，
  重启后重新调谐也只会补写状态，不会二次扣减。
- **过期释放 vs Pod 创建的竞争**：`Reserved→Bound` 的转移不在控制器里轮询，
  而是在 **Pod 准入 webhook 里原子完成**（resourceVersion 校验 + 冲突重试）。
  同一时刻「控制器做 `Reserved→Expired`」与「webhook 做 `Reserved→Bound`」
  只有一个能提交成功，输家读到已提交的终态后各自收敛（拒绝 Pod / 跳过过期）。
- **迟到 Pod**：过期后到达的 Pod 在准入阶段即被拒绝（fail-closed）；
  控制器在 `Expired` 态还会按 label 清扫漏网 Pod（防御纵深）。
- **一致性兜底**：`Bound` 后控制器用**非缓存读**检查 Pod（避免缓存滞后误释放），
  Pod 终止或被删即释放配额；删除 ResourceRequest 由 finalizer 保证配额先释放。

## Webhook 校验规则

`ValidatingWebhookConfiguration/quota-reserver-validating-webhook`，只对带
`quota.biaozhu.dev/managed-by=quota-reserver` 标签的命名空间生效，`failurePolicy: Fail`。

| Webhook | 规则 |
|---|---|
| `vresourcerequest.quota.biaozhu.dev` | `cpuMilli ∈ [1, 100000]`（毫核）、`memoryBytes ∈ [1, 100Gi]`（字节）、`ttlSeconds ∈ [10, 86400]`；spec 创建后不可变 |
| `vpod.quota.biaozhu.dev` | Pod 必须带 `quota.biaozhu.dev/request` 标签；引用的请求必须存在、处于 `Reserved` 且未过期；Pod 的 requests（按调度器口径：max(Σ容器, 最大init容器)）必须与预留量精确相等；准用时原子完成 `Reserved→Bound` |

Webhook 证书由管理器启动时**真实生成**（ECDSA P-256 自签 CA + 签发服务端证书，
`internal/cert`），写入 Secret 并把 CA 注入 webhook 配置的 `caBundle`，不依赖
cert-manager。两个副本首次启动时可能并发抢建 Secret，抢输的副本会回读 Secret
中的共享 CA（不会各发一套证书）；运行期每 60s 自愈一次 `caBundle`，因此重复
`kubectl apply` 清单不会让 webhook 永久失效。Deployment 默认 **2 副本**：只有
leader 做调谐，两个副本同时提供 webhook，删除/滚动其中一个时准入不中断。

## 目录结构

```
api/v1alpha1/          CRD 类型、校验逻辑、ResourceRequest 准入 webhook
pkg/reserve/           配额账本纯函数（幂等 Reserve/Release，可单测）
internal/controller/   ResourceRequest 控制器（状态机 + 乐观并发）
internal/webhook/      Pod 准入 webhook（原子绑定 Reserved→Bound）
internal/cert/         自签 CA / 服务端证书供给（真实密码学操作）
cmd/manager/           管理器入口
config/                CRD、RBAC、Deployment、Webhook 配置、示例
config/install.yaml    单文件安装清单（scripts/bundle.sh 生成）
test/envtest/          基于真实 apiserver（envtest）的集成测试
scripts/acceptance.sh  验收脚本（并发/绑定/过期/重启/不变式）
scripts/e2e-kind.sh    kind 一键端到端
```

## 本地启动

前置：Go 1.22+、docker、kind、kubectl。

```bash
# 1) 单元测试（无需集群）
make test-unit

# 2) 集成测试（envtest，自动下载 etcd + kube-apiserver 1.31）
make test-envtest

# 3) kind 端到端：建集群、构建镜像、部署、跑验收
make e2e            # 等价于 ./scripts/e2e-kind.sh
```

手动分步：

```bash
make generate manifests bundle   # 生成 deepcopy / CRD / install.yaml
kind create cluster --name quota-reserver-e2e
docker build -t quota-reserver:e2e .   # 构建机网络受限时加 --network=host
kind load docker-image quota-reserver:e2e --name quota-reserver-e2e
kubectl apply -f config/install.yaml
kubectl -n quota-system rollout status deployment/quota-reserver-controller-manager
```

## 验收命令

```bash
# 对当前 kubectl 上下文跑完整验收（A 并发 / B 绑定 / C 过期+迟到 Pod / D 重启 / E 不变式）
./scripts/acceptance.sh

# 共享机器上建议显式固定 context，避免写到别的集群：
KUBECTL="kubectl --context kind-quota-reserver-e2e" ./scripts/acceptance.sh

# 手动冒烟
kubectl apply -f config/samples/namespace.yaml
kubectl apply -f config/samples/quotapool_default.yaml
kubectl apply -f config/samples/resourcerequest_example.yaml
kubectl -n demo-quota get rrq example -w        # Pending -> Reserved
kubectl -n demo-quota get qpool default          # 观察 used*/allocations
kubectl apply -f config/samples/pod_example.yaml # 准入后 -> Bound
kubectl -n demo-quota delete pod example         # -> Released，配额释放
```

注：`scripts/e2e-kind.sh` 会把 `registry.k8s.io/pause:3.9` 预载进 kind 节点
（节点内通常无法直连公网镜像仓库）；手工部署时请先执行
`kind load docker-image registry.k8s.io/pause:3.9 --name <cluster>`，若该命令
因 containerd 镜像存储报错，可用
`docker save registry.k8s.io/pause:3.9 | docker exec -i <node> ctr -n k8s.io images import -`。

## 测试覆盖

| 场景 | 位置 | 方式 |
|---|---|---|
| 账本幂等/容量边界/释放 | `pkg/reserve/ledger_test.go` | 纯函数单测 |
| 单位与上限校验 | `api/v1alpha1/validation_test.go` | 表驱动单测 |
| 资源版本冲突重试 | `internal/controller/*_test.go`、`internal/webhook/*_test.go` | 注入 Conflict 错误的 fake client |
| 状态机各转移、finalizer、迟到 Pod 清扫 | `internal/controller/*_test.go` | fake client |
| 并发申请不超卖（20 并发 → 恰好 6 预留） | `test/envtest` | 真实 apiserver，MaxConcurrentReconciles=4 |
| 重复调谐/状态丢失不重复扣减 | `test/envtest` | 真实 apiserver |
| 控制器重启后收敛 | `test/envtest` | 停旧 manager 起新 manager |
| Webhook 拒绝（越界/改 spec/无标签/不匹配/已过期/Pending） | `test/envtest` | 真实 apiserver + 真实 webhook |
| 绑定-释放全生命周期一致性 | `test/envtest` + `scripts/acceptance.sh` | envtest 与 kind 双层 |
| 过期后迟到 Pod 被拒 | `test/envtest` + `scripts/acceptance.sh` | 同上 |
| 控制器 Pod 重启（进程级） | `scripts/acceptance.sh` 场景 D | kind 上删除控制器 Pod |

## 设计权衡与限制

- 一个命名空间一个池（约定名 `default`），请求通过 `spec.pool` 选择；多池可扩展。
- Pod 的 requests 必须与预留**精确相等**（防止少报多占）；limits 不校验。
- 预留过期以控制器时钟为准；webhook 与控制器同集群部署，时钟偏差风险可忽略。
- `failurePolicy: Fail`：webhook 不可用时受管命名空间的 Pod/请求创建会失败（fail-closed）。
