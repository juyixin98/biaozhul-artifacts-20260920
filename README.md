# 读写仲裁与读修复模拟器（N/W/R Quorum Demo）

用 Go 标准库（`net/http`）实现的**纯后端** N 副本读写仲裁（quorum）模拟器。
每个副本是内存版本存储，版本带**向量钟（vector clock）**；并发写产生
不可比较的兄弟版本（siblings）并作为冲突如实返回；读时合并、剪枝并做
**读修复（read repair）**，另提供手工反熵（anti-entropy）接口模拟副本恢复。

无界面，仅 HTTP/JSON 接口 + curl 演示脚本。**零第三方依赖**。

## 依赖与环境

| 项 | 版本/说明 |
| --- | --- |
| Go | 1.23（开发实测 `go1.23.4 linux/amd64`；仅用标准库，更低 1.21+ 大概率可用，未逐一验证） |
| 第三方 Go 依赖 | **无**。`go.mod` 不 require 任何外部模块；没有 `go.sum`，这本身即“依赖锁定”：任何机器 `go build` 拉取的代码都只有标准库 |
| 演示脚本 | `bash`、`curl`、`python3`（仅 demo.sh 中自动抽取冲突向量钟用；不用脚本则不需要） |

## 启动命令

```bash
# 在项目根目录
go run ./cmd/server                      # 默认 :8080, N=3 W=2 R=2
# 或编译后运行
go build -o quorum-server ./cmd/server
./quorum-server -addr :8080 -n 3 -w 2 -r 2 -rpc-delay 10ms -deadline 60ms
```

参数：

- `-n` 副本数 N；`-w` 写法定人数 W；`-r` 读法定人数 R（均要求 1 ≤ W,R ≤ N）
- `-rpc-delay` 一次成功副本 RPC 的模拟往返延迟（默认 10ms）
- `-deadline` 协调者等待 W/R 个响应的截止时间（默认 60ms）。宕机副本永不
  响应，因此 deadline 到期就是“部分写成功后超时”

健康检查：`curl http://127.0.0.1:8080/health`

## 一键演示（三个验收场景 + 一个对照实验）

```bash
go run ./cmd/server &        # 先启动服务
./demo.sh                    # 自动执行全部场景，打印每步状态码与 JSON
```

也可指定地址：`BASE=http://127.0.0.1:9000 ./demo.sh`

## HTTP 接口与请求样例

所有请求/响应均为 JSON。状态码约定：`200` 正常；`400` 参数错误；
`404` 法定人数内无此 key；`409` 读到并发兄弟版本（冲突）；
`504` 未在 deadline 内凑齐 W/R（响应体仍含真实的部分结果）。

### 配置与故障注入

```bash
curl -s localhost:8080/config            # GET 查看 N/W/R、节点、宕机集合
curl -s -X POST localhost:8080/config -H 'Content-Type: application/json' \
  -d '{"w":1,"r":1}'                     # 运行期改 W/R（用于 W+R<=N 对照）
curl -s -X POST localhost:8080/replicas/down -d '{"node":"n3"}'
curl -s -X POST localhost:8080/replicas/up   -d '{"node":"n3"}'
curl -s -X POST localhost:8080/reset      # 清空数据与故障状态
```

### 写（W 仲裁）

```bash
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8080/write \
  -H 'Content-Type: application/json' \
  -d '{"key":"k","value":"v1","coordinator":"n1"}'
# 可选字段：nodes（只接触部分副本）、deadline_ms、parents（带上下文写的父钟）
```

响应如实给出 `acked_by`（最终真正持久化的副本）、`timed_out_by`、
`quorum_met`。**超时的写不回滚**——已落地副本保留该版本。

### 读（R 仲裁 + 读修复）

```bash
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8080/read \
  -H 'Content-Type: application/json' -d '{"key":"k"}'
# 可选：nodes、no_repair、deadline_ms
```

`versions` 是合并剪枝后的最大版本集：长度 1 = 唯一最新值；长度 >1 =
冲突（HTTP 409），每个版本带自己的向量钟。达成 R 时默认把完整最大版本集
修复到所有落后但可达的副本（`repaired_to` 列出被修复者）。

### 冲突消解（客户端裁决）

```bash
curl -s -X POST localhost:8080/resolve -H 'Content-Type: application/json' -d '{
  "key":"k",
  "value":"winner",
  "coordinator":"n3",
  "siblings":[{"n1":1},{"n2":1}]
}'
```

新钟 = 合并所有兄弟钟后协调者分量 +1；后继版本在因果上支配所有兄弟，
之后读只剩它（旧兄弟仍保留在副本历史中）。

### 反熵修复（副本恢复）

```bash
curl -s -X POST localhost:8080/repair -H 'Content-Type: application/json' \
  -d '{"key":"k"}'                        # 用在线副本并集的最大版本追平全部可达副本
curl -s 'localhost:8080/state?key=k'      # 查看每个副本上该 key 的真实版本
```

## 验收场景与实际运行结果

> 以下结果于 2026-09-24 在本机实跑得到（`go test ./...` 与 `./demo.sh`）。
> 版本 ID 含随机后缀，每次运行不同；结构与结论稳定。可自行复现。

### 1. 部分写成功后超时 → 恢复 → 读修复

- 宕掉 n2、n3；`W=2` 写 `k=v1`：**HTTP 504**，`acked_by=["n1"]`、
  `timed_out_by=["n2","n3"]`、`quorum_met=false`。n1 上版本
  `clock={n1:1}` 已真实持久化（写不回滚）。
- 此时读同样 **504**（只收到 n1，不足 R=2），系统明确拒绝据此断言最新值。
- 恢复 n2、n3 后读：**200**，读到唯一版本 `v1 {n1:1}`，
  `repaired_to=["n2","n3"]`；`/state?key=k` 可见三个副本持有同一版本 ID。

### 2. 并发写 → 实际读到版本与冲突 → 消解

- 写 A 只接触 `{n1,n2}`（钟 `{n1:1}`），写 B 只接触 `{n2,n3}`（钟
  `{n2:1}`），两次写无因果关系，向量钟不可比较。
- 读返回 **HTTP 409**、`conflict=true`、两个兄弟版本 `A`/`B` 原样给出，
  系统不替客户端挑选，响应 `note` 明确写出“这些历史不构成线性一致”。
- `/resolve`（父钟 `[{n1:1},{n2:1}]`，协调者 n3）生成 `{n1:1,n2:1,n3:1}`
  的 `winner`；再读为 **200** 且只剩 `winner`（测试断言它在因果上后于两个兄弟）。

### 3. 副本恢复 + 反熵

- n3 宕机期间写 `v1`（n1、n2 确认），响应中 `timed_out_by=["n3"]`。
- 恢复 n3（持空数据）后 `/repair`：`updated=["n3"]`，三副本收敛到同一版本；
  再次 `/repair` 的 `updated=[]`（幂等收敛）。

### 对照：W+R<=N 时可以读旧值

切到 `W=1,R=1`（W+R=2 ≤ 3）：写只落 n1，只读 n2 返回 **404**
（读集合与写集合不相交），只读 n1 才 200。说明连“读到最近已完成写”都不
保证，更谈不上线性一致。

## 语义边界：为什么 W+R>N 不能自动保证所有并发语义

这是本项目要明确表达的核心结论，代码中（409 响应、`/config` 提示）也反复强调：

1. **W+R>N 只保证法定人数集合相交**：任意 R 个副本的读集合与最近一次
   *已完成*写的 W 个副本至少有一个交点，因此“读不会整体停留在该写之前”。
2. **它不解决并发写冲突**。两次并发写各自满足 W 是完全可能的
   （本仓库场景二即在 W=R=2、N=3 下构造出并存的 A/B）。交点副本同时持有
   两者，合并且剪枝后仍是多个不可比较版本。仲裁公式里没有任何东西能凭空
   决定 A、B 谁先谁后——这需要向量钟检测 + 客户端裁决（last-write-wins、
   业务合并、CRDT 等），本模拟器一律保留冲突并提供 `/resolve`。
3. **它不自动产生线性一致历史**。线性一致要求所有操作看起来按某个与真实
   时间一致的全序原子执行。W+R>N 下仍存在：写超时但部分落地（成功/失败
   对客户端是未定的）、新旧读重排、跨多个 key 无事务顺序等。单 key、无
   故障、严格法定人数的特定配置可以接近某些一致性表述，但模拟器**不会**
   把未证明的历史标成线性一致——只有向量钟下因果有序的版本才被剪枝，
   并发的一律标为 conflict。
4. 需要更强语义的常见做法（本项目未实现，见“未完成项”）：领导者/租约
   （单一写入口得到线性一致写）、读时做 R + read-repair 后再返回并配合
   同步副本集、或在冲突解决层引入全序器。

## 代码结构与测试

```
go.mod                模块定义；零第三方依赖（无 go.sum 即锁定状态）
quorum/clock.go       向量钟：因果先于、并发、合并、稳定渲染
quorum/cluster.go     副本、N/W/R 仲裁读写、超时、读修复、反熵、冲突剪枝
api/server.go         net/http JSON 接口与状态码语义
cmd/server/main.go    启动入口（flag 配置 N/W/R 与模拟时延）
demo.sh               三个验收场景 + W+R<=N 对照的 curl 脚本
quorum/*_test.go      向量钟/剪枝单元测试；三个验收场景的集群级测试；
                      W+R<=N 旧读对照；配置校验
api/server_test.go    httptest 端到端测试：504 部分写、409 冲突、404/400、
                      运行期重配 W/R
```

```bash
go test ./...            # 全部自动化测试
go test -race ./...      # 含竞态检测（已验证通过）
go test -v ./...         # 查看逐用例结果
go vet ./...
```

测试覆盖的关键不变量：

- 超时部分写不回滚、恢复后读修复把相同版本 ID 写到落后副本；
- 并发写的钟被判定为不可比较，读必须返回 2 个兄弟而**不能**擅自选一个；
- resolve 后继版本在向量钟上严格后于两个兄弟；
- 反熵修复幂等；
- W=1/R=1 下不相交集合确实读到旧值（用测试固化“不保证线性一致”的结论）。

## 建模上的简化与取舍

- 所有副本在同一进程内、用 goroutine + 通道模拟 RPC；故障 = 不响应
  （丢包/超时），不模拟拜占庭行为、网络分区恢复后的再平衡。
- RPC 延迟为固定值（10ms），不做随机分布；写在 W 个确认后立即返回，
  响应统计会等待在途 RPC 落地，以如实展示“最终哪些副本有数据”。
- 读修复采用激进策略：对所有可达落后副本同步修复（真实 Dynamo 通常是
  部分修复 + 后台 Merkle 反熵兜底，本项目的 `/repair` 对应后者）。
- 版本永不物理删除，便于用 `/state` 观察；长期运行的垃圾回收（向量钟
  剪枝/tombstone）未实现。

## 未完成项 / 已知限制

- 未实现跨 key 事务、多 key 快照读；所有一致性讨论限定在单 key。
- 未实现真正的后台反熵线程与 Merkle 树（只有按需 `/repair`）。
- 未实现版本/tombstone GC、 hinted handoff、成员变更（改 N 需重启）。
- 无鉴权、无持久化（进程退出数据丢失）——定位就是教学模拟器。
- 冲突解决策略只演示“客户端显式裁决 + 因果后继版本”；未内置 LWW/CRDT。
