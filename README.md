# config-distributor — 控制器事件去重演示

一个基于 **Go + controller-runtime + kind** 的纯后端 Kubernetes 控制器：把一份
**不可变配置** 按命名空间选择器分发到各目标命名空间，子对象以 **配置摘要命名**，
重复事件、全量 resync、进程重启都不会产生重复对象；选择器缩小时只清理
**本资源（按 UID 判定）拥有** 的对象；状态 **逐目标记录版本**，部分失败只重试
失败目标、绝不让成功目标回退。

## 设计要点（事件去重）

| 机制 | 实现 |
|---|---|
| 幂等命名 | 子 ConfigMap 名 = `<cr名>-<sha256(config)前12位>`，是期望状态的纯函数；先 Get 后 Create，重复事件/resync/重启全部收敛为 no-op |
| 摘要计算 | `ConfigDigest()`：键排序后做长度前缀序列化再 sha256，与 map 迭代顺序无关（真实计算，非模拟） |
| 所有权判定 | 子对象带 `owner-uid` 标签 + controller ownerReference，清理时 **双重校验 UID**，UID 不同的同名对象绝不误删 |
| 所有者重建 | CR 被删（finalizer 被强制移除）后同名重建（新 UID）：孤儿对象被 **收养**（更新 ownerRef/标签），不删除不重建 |
| 命名冲突 | 外部对象占用确定性名称：标记 `Conflict` 并周期重试，**不覆盖、不删除** 外部对象 |
| 部分失败 | 状态 `targets[]` 逐命名空间记录 digest/phase/message；失败目标 `Failed`+退避重试，成功目标保持 `Ready` 且不被重写 |
| 不可变配置 | CEL 校验 `self == oldSelf`，API 服务器直接拒绝修改（kind v1.30 真实执行） |
| 删除清理 | finalizer 删除全部自有子对象后放行；K8s GC 级联作为兜底 |

CR 为 **集群作用域**：子对象分布在多个命名空间，跨命名空间 ownerReference
非法（会被 GC 误删），集群作用域 owner 引用命名空间从属对象是合法拓扑。

## 目录结构

```
api/v1alpha1/                  CRD 类型 + 生成的 deepcopy
cmd/manager/                   控制器入口
internal/controller/           调和逻辑 + 单元测试（fake client）
config/crd/bases/              生成的 CRD 清单（含 CEL 不可变校验）
config/rbac/                   生成的 ClusterRole
config/samples/                示例输入（命名空间 + ConfigDistribution）
hack/e2e.sh                    端到端演示脚本（8 个场景）
artifacts/                     e2e 运行产物与失败证据（运行时生成）
```

## 本地启动

前置：Go 1.22+、docker、kind、kubectl。

```bash
# 1. 创建集群并安装 CRD
kind create cluster --name p090b --image kindest/node:v1.30.10
kubectl apply -f config/crd/bases/dist.example.com_configdistributions.yaml

# 2. 本地运行控制器（使用当前 kubeconfig）
make run          # 等价于 go run ./cmd/manager

# 3. 另开终端：创建示例命名空间并下发配置
kubectl apply -f config/samples/namespaces.yaml
kubectl apply -f config/samples/dist_v1alpha1_configdistribution.yaml

# 4. 验收
kubectl get cfds main -o yaml        # 查看 status.targets 逐目标版本
kubectl get cm -A -l app.kubernetes.io/managed-by=config-distributor
```

## 验收命令

```bash
make test     # 单元测试：乱序事件/去重/所有者重建/命名冲突/部分失败/重启/删除
make e2e      # 端到端：新建 kind 集群跑 8 个场景，失败证据保留在 artifacts/
make e2e-clean
```

`make e2e` 覆盖的场景（任一失败即保留现场并非零退出）：

1. 按选择器分发，子对象以摘要命名
2. 重复 apply + 注解触发 resync → 无重复、UID 不变
3. `kubectl patch` 修改 config → 被 CEL 拒绝
4. 带外删除子对象 → 自动重建（乱序安全）
5. 选择器先扩大遇到外部同名对象 → `Conflict` 且对象原样保留，兄弟目标保持
   `Ready`；冲突解除后收敛；再缩小选择器 → 仅删除自有对象
6. 所有者重建（先停控制器模拟崩溃，再强制移除 finalizer 并以
   `--cascade=orphan` 删除 CR，随后同名重建）→ 孤儿被收养，UID 不变。
   注意：若控制器在线时移除 finalizer，控制器会在下一次调和时重新补上
   finalizer，随后的删除会正常走 finalizer 清理路径——这不是 bug，而是
   finalizer 的设计行为，因此崩溃场景必须在控制器停止后模拟。
7. 杀掉控制器进程重启 → 纯 no-op
8. 删除 CR → 自有子对象全部回收，外部对象保留

## 依赖锁定

`go.mod` / `go.sum` 已锁定：controller-runtime v0.18.4、client-go v0.30.3，
与 kind 节点镜像 v1.30.10 对齐。`controller-gen` 版本固定在 Makefile
（v0.16.5），`make generate` 可复现全部生成物。
