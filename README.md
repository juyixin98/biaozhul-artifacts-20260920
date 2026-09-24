# quorumlab — N 副本读写仲裁与读修复模拟（Go / net/http，纯后端）

一个只依赖 Go 标准库的内存模拟服务：在单进程内模拟 N 个副本、可配置的
**写仲裁 W / 读仲裁 R**、**向量时钟版本**、**多值（siblings）冲突保留**、
**读修复（read repair）**、**副本故障注入与恢复（反熵）**。

没有任何界面，只提供 HTTP/JSON 接口。

---

## 1. 它到底保证什么、不保证什么（先读这个）

**W+R > N 保证的唯一件事**：某次“收到 W 个确认的写”所用副本集合，与某次
“收到 R 个响应的读”所用副本集合**必然相交**（若不相交，则需要 W+R 个不同
副本，与总共只有 N 个矛盾）。因此：

- 一个**已经达到写仲裁**的版本，后续任何达到读仲裁的读都不会漏掉它（
  read-your-write/单调新一类性质在此前提下成立）。

**W+R > N 不自动保证的事**：

- 它**不**给并发写排定全局顺序。两个互不知晓对方的盲写（向量时钟并发）
  即使都达到了 W，系统也无法判断“谁先谁后”——本模拟器会把两者都保留为
  **兄弟版本（siblings）并报告 `conflict=true`**，而不是用时间戳/副本号
  之类的假象静默选一个。
- 它**不**自动等于**线性一致性（linearizability）**。线性一致要求操作有
  实时（real-time）全序；仅凭读写副本集合相交证明不了这种全序的存在，还
  需要领导者 + Raft/Paxos 之类的协议。**本模拟器绝不把未证明的历史标记
  为线性一致**——每个响应都带 `consistency_note` 重申这一点。
- 若写**没有达到 W**（部分成功后协调者超时），这个版本只是副本上的
  **未确认意向**；读看到它时会明确给出 `committed=false` 与
  `has_uncommitted=true`，你必须把它当“未定历史”处理，而不是已提交数据。
- W+R ≤ N 时，连“读不漏掉已确认写”都不保证（读写副本集合可以不相交）。

并发安全的补法（本模拟器不实现，只提供解决原语）：让客户端在写时携带
读到的向量时钟（因果上下文），或对冲突做显式合并（`/resolve` 即以全部
兄弟版本为共同因果后继写一次）。

---

## 2. 依赖与环境

| 项 | 值 |
|---|---|
| 语言 | Go（开发/实测版本 **go1.23.4 linux/amd64**，`go.mod` 要求 go ≥ 1.22，因使用 Go 1.22 的 `METHOD /path` 路由） |
| 第三方依赖 | **无**。仅 `net/http`、`encoding/json`、`sync`、`time` 等标准库 |
| 构建/联网 | 构建与测试不需要访问模块代理；`go.sum` 不存在是正常的（没有任何外部模块可供校验），`go mod verify` 对纯标准库构建无对象可校验 |
| 演示脚本 | `bash`、`curl`，可选 `jq`（没有时回落到 `python3 -m json.tool`） |

“锁定依赖”的方式：零第三方依赖 + `go.mod` 固定工具链下限，因此不存在需要
版本钉死的间接依赖；`go mod tidy` 不会产生任何变更。

## 3. 启动

```bash
# 直接运行
go run . -n 5 -w 3 -r 3 -addr :8080
# 或先编译
go build -o quorumlab . && ./quorumlab -n 5 -w 3 -r 3 -addr :8080
```

参数：`-n` 副本数（默认 5）、`-w` 写仲裁（3）、`-r` 读仲裁（3）、
`-addr` 监听地址（`:8080`）。要求 1 ≤ W,R ≤ N。

健康检查/查看参数：

```bash
curl -s http://127.0.0.1:8080/config
# {"n":5,"w":3,"r":3}
```

## 4. 测试

```bash
go test -race -count=1 ./...      # 竞态检测 + 全部单元/HTTP 测试
go test -cover ./...              # 覆盖率
```

测试文件：`cluster_test.go`（向量时钟、剪枝、部分写超时、读修复、并发
冲突、冲突解决、副本恢复、参数校验）、`server_test.go`（`httptest` 端到端：
200/503/504 状态码、冲突解决、恢复、错误输入、慢副本晚到落地）。

一键端到端演示（自动起服务、依次跑完第 6 节的全部验收场景）：

```bash
./demo.sh                 # 自动选 /usr/local/go/bin/go，监听 127.0.0.1:19090
GO_BIN=$(command -v go) PORT=20000 ./demo.sh   # 自定义 go 与端口
./demo.sh http://127.0.0.1:8080                 # 或对接已运行的实例
```

## 5. HTTP 接口

所有请求/响应均为 JSON。写失败但可能部分落地时返回 **504**（body 仍是
完整的 `WriteOutcome`）；读未凑齐 R 时返回 **503**（body 仍给出部分数据）。

| 方法 路径 | 说明 | 关键字段 |
|---|---|---|
| `GET /config` | 查看 N/W/R | `n,w,r` |
| `PUT /config` | 改 W/R（改 N 会重建并清空副本，返回 `replicas_reset`） | `n,w,r` |
| `POST /write` | 协调一次写 | `key,value,context?,coordinator?,w?,targets?,timeout_ms?` |
| `GET /read?key=...` | 协调一次读 | 查询参 `r,targets,timeout_ms,repair,full` |
| `POST /resolve` | 冲突解决（因果后继写） | `key,value,merge_all` 或 `version_ids` |
| `POST /fault` | 故障注入 | `replica,mode=up\|down\|delay,delay_ms?` |
| `POST /recover` | 副本恢复（反熵） | `replica,from?` |
| `GET /state` | 全部副本状态与数据快照 | — |
| `POST /reset` | 清空全部数据与故障 | — |

### 响应要点

写 `WriteOutcome`：`clock`（本次写的向量时钟）、`acked`、`quorum`、
`committed`、`timeout`、`results[]`（每副本 `acked/timeout/error`）。

读 `ReadOutcome`：`responses`、`quorum`、`found`、`conflict`（兄弟版本
>1 即真冲突）、`value`（仅单版本时给出）、`versions[]`（每个版本带
`clock` 与 `committed`）、`has_uncommitted`、`repaired_to[]`。

向量时钟语义：分量 `w<序号>` 是一次写的逻辑计数；两个时钟一个支配另一个
即为因果先后，互不支配即为**并发**（冲突）。`context` 给出本次写已知的
全部历史；省略/空对象表示一次新的盲写分支。

## 6. 请求样例（与 `demo.sh` 一致）

假设服务在 `$B=http://127.0.0.1:8080`。

```bash
# (0) 重置
curl -s -X POST $B/reset -d '{}'

# (1) 普通写 / 读
curl -s -X POST $B/write -H 'Content-Type: application/json' \
  -d '{"key":"color","value":"blue"}'
curl -s "$B/read?key=color"

# (2) 部分写成功后超时：3,4 down；2 慢 800ms；W=3；协调者只等 200ms
curl -s -X POST $B/fault -d '{"replica":3,"mode":"down"}'
curl -s -X POST $B/fault -d '{"replica":4,"mode":"down"}'
curl -s -X POST $B/fault -d '{"replica":2,"mode":"delay","delay_ms":800}'
curl -s -i -X POST $B/write -d '{"key":"split","value":"maybe","timeout_ms":200}'
#   → HTTP 504，acked=2, quorum=false, timeout=true；0,1 已落地未确认
curl -s "$B/read?key=split&r=1&targets=0"     # committed=false, has_uncommitted=true

# (3) 并发写：两次盲写都达仲裁，读看到真实冲突（即使 W+R=6 > N=5）
curl -s -X POST $B/write -d '{"key":"race","value":"A","targets":[0,1,2]}'
curl -s -X POST $B/write -d '{"key":"race","value":"B","targets":[0,3,4]}'
curl -s "$B/read?key=race&targets=1,3,4&r=3"  # conflict=true，两个并发版本

# (4) 读修复：写只到 1,2，从 1,3 读并修复，3 被补齐
curl -s -X POST $B/write -d '{"key":"rr","value":"fixed","w":2,"targets":[1,2]}'
curl -s "$B/read?key=rr&targets=1,3&r=2&repair=1"   # repaired_to 含 3

# (5) 副本恢复
curl -s -X POST $B/fault -d '{"replica":4,"mode":"down"}'
curl -s -X POST $B/write -d '{"key":"back","value":"online","targets":[0,1,2]}'
curl -s -X POST $B/recover -d '{"replica":4}'        # keys_pulled 列出补齐项

# (6) 解决冲突：以全部兄弟版本为共同因果后继写一次
curl -s -X POST $B/resolve -d '{"key":"race","value":"A-then-B-resolved","merge_all":true}'
curl -s "$B/read?key=race&repair=1&full=1"           # conflict=false，单版本
```

## 7. 目录结构

```
go.mod            模块定义（无第三方依赖）
main.go           入口/参数
cluster.go        副本、向量时钟、仲裁协调、读修复、恢复（核心）
server.go         net/http 接口层
cluster_test.go   核心逻辑测试
server_test.go    HTTP 端到端测试
demo.sh           验收场景脚本
TEST_RESULTS.md   本机实测记录
README.md         本文件
```

## 8. 模拟器的建模取舍（务必知晓）

- 所有“副本 RPC”都是单进程内加锁的方法调用 + 可注入的 `down/delay`，
  不模拟真实网络丢包、乱序、重复消息；`delay` 副本在协调者超时后仍会把
  意向落地（模拟晚到的响应），这一效果可被读/`/state` 观测。
- 存储为内存态，重启即清空。
- 写达到 W 后，协调者对已确认副本同步 best-effort 传播 `committed` 标记
  （每个副本最多等 500ms）；没来得及标记的副本会在**读修复**时补齐。
- 读修复默认只修复本次响应过的副本；`full=1` 时修复全部健康副本。
- 不做成员变更：运行期改 N 会重建空集群（接口会如实返回 `replicas_reset`）。
- 未实现真正的共识（Raft/Paxos）、租约或领导者线性一致读——这正是本项目
  要展示“W+R>N 不足够”的原因，而不是缺陷。
