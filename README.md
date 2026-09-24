# 控制器事件去重 — ConfigSnapshot 配置分发控制器

一个使用 **Go + controller-runtime + kind** 的纯后端项目：把一份**不可变配置**按
namespace 标签选择器分发给所有测试命名空间。核心解决的是控制器在**重复事件、全量
resync、乱序事件、所有者重建、命名冲突和进程重启**下的正确性问题。

- 子对象（ConfigMap）以**配置内容摘要（sha256）命名**，天然幂等：重复事件与全量
  resync 不会重复创建。
- 选择器缩小时只清理**确属本资源 UID** 的对象；同名但 owner UID 不同的对象绝不删除。
- 状态**逐目标**记录内容版本；某个目标失败并重试，不会回退其他已成功目标的版本。
- 通过一个真实的 admission webhook（`FailPolicy` CRD）注入故障，得到可重复的
  “部分失败”场景，而不是在代码里伪造错误。
- 所有 TLS 证书由管理器用真实密码学操作（ECDSA P-256 + x509）自签生成。

## 架构

```
ConfigSnapshot (config.example.com/v1alpha1, cluster-scoped)
  spec.payload.data/format   —— 不可变（validating webhook 强制）
  spec.selector              —— namespace 标签选择器（可收窄，触发 GC）
  status.targets[]           —— 逐目标 phase/version/childName/lastError

        │ reconcile（For CS / Watches Namespace / Owns ConfigMap）
        ▼
每个被选中 namespace 一个 ConfigMap：cfg-<owner>-<sha256前16位>
  - labels:      app.kubernetes.io/managed-by=config-distributor
                 config.example.com/owner=<owner>
  - annotations: config.example.com/digest=<完整64位摘要>
  - ownerReferences.controller.uid = ConfigSnapshot 的真实 UID（归属判定权威依据）

FailPolicy (chaos.example.com/v1alpha1)  ──► chaos validating webhook (/inject)
  按 namespace / labels / 对象名 / 操作(VERB) 匹配，Always 或 FirstN 拒绝请求
```

关键不变量（代码与测试都围绕它们）：

1. **去重**：reconcile 先 Get 子对象，存在且内容一致即 no-op；不存在才 Create。
   名字由 `sha256(canonical(format,data))` 决定，所以 N 次事件 = 1 次 Create。
2. **GC 的 UID 守卫**：缩窄选择器时，按 label 列出候选，仅当
   `ownerReferences[*].uid == 当前 ConfigSnapshot.uid` 才删除。label 相同但 UID
   不同（旧 owner 残留 / 同名抢占）一律跳过。
3. **命名冲突安全**：期望名字被一个非本 UID 的对象占用时，目标置为 `Failed`
   并记录事件，**绝不删别人的对象**。
4. **逐目标版本不回退**：每个目标的 `version` 只在该目标成功时前进到新摘要；
   失败时保留该目标上一个成功版本（从未成功则为空），其他目标完全不受影响。
5. **重启幂等**：所有期望状态可从 API server 重建；重启后重新收敛，不产生重复对象。

## 目录结构

```
api/v1alpha1/                 ConfigSnapshot API 类型 + 手写 deepcopy
internal/naming/              sha256 摘要、确定性子对象名
internal/controller/          核心 reconciler（去重/GC/UID 守卫/逐目标状态）
internal/failinject/          FailPolicy CRD + matcher + chaos/validate webhook
internal/failinject/certs/    真实 ECDSA+x509 自签 CA/服务证书
cmd/manager/                  管理器入口（控制器 + 两个 webhook）
config/crd/bases/             两个 CRD
config/rbac|manager|webhook/  RBAC、Deployment、Service、ValidatingWebhookConfiguration
config/samples/               示例输入
hack/deploy.sh                kind 建集群 + 构建加载镜像 + 部署 + CA 注入
hack/e2e.sh                   可重复端到端演示（7 个场景，保存证据）
test/evidence/<timestamp>/    每次 e2e 的状态、日志、事件等证据
```

## 前置要求

- Go 1.22+
- Docker
- kind v0.20+、kubectl（在 kind v0.23 / Kubernetes v1.30 上验证）
- `jq`、`base64`、`openssl`（仅调试时）

## 本地启动（一键）

```bash
make deploy          # = hack/deploy.sh
```

脚本会：建 `config-dedup` kind 集群 → 构建并 `kind load` 镜像 → 安装 CRD/RBAC →
部署管理器 → 等待就绪后从 Pod 中读取其自签 CA，注入两个
ValidatingWebhookConfiguration → 创建测试命名空间。

> Docker 构建默认使用 `--network=host`，以便构建容器能访问 Go module 代理。

## 验收

### 1) 单元测试（不依赖集群）

```bash
make test            # go test ./...
make test-race       # 竞态检测
make vet
```

覆盖：5 轮 reconcile 恰好 1 次 Create/目标；选择器收窄的 UID 守卫；部分失败下
成功目标版本保留与故障清除后收敛；漂移原地修复；同名新 UID 重建 owner 不删旧
orphan；FailPolicy 的 FirstN/标签/动作匹配与计数；admission 协议拒绝/放行；
校验 webhook 的不可变性；以及真实证书链的 TLS 握手验证。

### 2) 端到端演示（真实 kind 集群 + 真实故障注入）

```bash
make deploy          # 首次需要
make e2e             # = hack/e2e.sh
```

脚本顺序执行并逐项断言（全部通过才退出码 0）：

1. 初始分发到 `test-a/b/c`，校验 payload 与 controller ownerReference；
2. 50+ 次重复/乱序事件 + namespace 抖动触发重算 → 子对象 UID、resourceVersion
   完全不变（无重建）；
3. 对 `test-b` 下 ConfigMap 的 CREATE 注入 **Always** 拒绝 → `test-b=Failed` 且
   `lastError` 为真实 apiserver Forbidden，`test-a/test-c` 版本与对象数不变；
   删除 FailPolicy 后定时重试收敛，三者同一摘要；
4. 把 `test-c` 摘标签 → 仅本 UID 子对象被 GC，其余命名空间不动；
5. 在 `test-c` 预置一个**同名、带相同 label、但 owner UID 不同**的外来
   ConfigMap → 控制器报告 `name conflict` 且外来对象原样保留；删除外来对象后
   自动收敛；
6. 删除并以同名重建 ConfigSnapshot（apiserver 分配新 UID），并保留一个旧 UID
   orphan → 新 owner 永不删除该 orphan，只安全地报 Failed；
7. 删除管理器 Pod（进程重启）→ 重新收敛后子对象总数仍为 3、failedCount=0。

证据保存在 `test/evidence/<时间戳>/`：

- `run.log`、`results.txt`（PASS/FAIL 汇总）
- `01/03/07-status*.json`、`08-final-configsnapshot.yaml`
- `controller.log`（控制器日志，含注入拒绝记录）
- `09-name-conflict-events.txt`、各 namespace 的 ConfigMap YAML、`SHA256SUMS`

### 3) 手工试一下

```bash
kubectl --context kind-config-dedup apply -f config/samples/configsnapshot.yaml
kubectl --context kind-config-dedup get configsnapshot demo-app -o yaml
kubectl --context kind-config-dedup get cm -A -l config.example.com/owner=demo-app

# 不可变性：以下更新应被 validating webhook 拒绝
kubectl --context kind-config-dedup patch configsnapshot demo-app --type=json \
  -p='[{"op":"replace","path":"/spec/payload/data","value":"changed\n"}]'
```

## 设计说明

- **为什么用摘要命名而不是固定名字**：内容不可变 + 名字含内容摘要，使“是否已
  分发这份内容”退化为一次按名 Get，重复事件/resync/重启都天然幂等；新版本是新
  对象名，不会原地覆盖造成版本混乱。
- **为什么归属以 UID 而不是 label/名字为准**：label 可被任何人伪造，名字在
  owner 同名重建时也会重复。Kubernetes ownerReference 的 UID 在对象生命周期内
  全局唯一，是“这个子对象到底是不是我创建的”的唯一可靠证据。
- **为什么“同名不同 UID”的 e2e 用一个真实的第二个 ConfigSnapshot 做外来 owner**：
  Kubernetes 自身的垃圾回收器会删除任何 ownerReferences 指向**不存在 UID** 的
  对象（与 `controller` 标志无关，普通引用也一样）。所以一个凭空捏造的 UID 在
  到达控制器之前就会被 kube GC 清掉，无法构成有效的冲突样本。用一个真实存在但
  不同的 owner，才是“同名、同 label、不同归属 UID”的忠实模型。控制器面对它的
  行为不变：只认自己的 UID，报 `name conflict`，绝不删除。
- **为什么故障用 webhook 而不是 mock**：端到端必须走真实的 apiserver 拒绝路径，
  控制器看到的是带真实 StatusError 的写失败，重试与状态行为才可信。
- **故障安全**：两个 webhook 的 `failurePolicy` 都是 `Ignore`；matcher 无策略时
  默认放行，故障注入面不可能把集群本身搞挂。管理器还持续把自签 CA 协调进
  ValidatingWebhookConfiguration（每 3s），VWC 被删除重建后也会自愈。

## 需求与测试对照

| 要求 | 单元测试 | e2e 场景 |
| --- | --- | --- |
| 重复事件/全量 resync 不重复创建 | `TestReconcile_CreatesOnceOnRepeatedEventsAndResync`（5 轮仅 1 次 Create） | 场景 2（50+ 事件后 UID/RV 不变） |
| 以配置摘要命名子对象 | `naming.TestContentDigest_*` / `TestChildName_*` | 场景 1（64 位摘要 + 名字后缀） |
| 选择器缩小只清理本 UID 对象 | `TestReconcile_SelectorNarrowingGCsOnlyOwnedObjects` | 场景 4、4b |
| UID 不同同名对象不得误删 | 同上（外来 UID 对象存活） | 场景 4b、6 |
| 所有者重建 | `TestReconcile_OwnerRecreatedWithNewUID` | 场景 6 |
| 命名冲突安全处理 | 同上（Failed，不删除） | 场景 4b |
| 逐目标版本、部分失败不回退 | `TestReconcile_PartialFailureKeepsSuccessfulTargetVersion` | 场景 3（真实 Forbidden + 版本保留） |
| 乱序/漂移事件幂等 | `TestReconcile_DriftIsRepaired` | 场景 2 |
| 进程重启 | — | 场景 7 |
| 真实故障注入协议 | `matcher_test` / `webhook_test` | 场景 3（FailPolicy admission 拒绝） |
| 不可变 payload | `validate_test.go` | README 手工命令（webhook 拒绝） |
| 真实密码学（TLS/证书） | `certs.TestEnsureCerts_RealChainAndTLSHandshake` | 部署阶段 CA 注入 + webhook 真实生效 |

## 清理

```bash
make kind-down       # kind delete cluster --name config-dedup
```
