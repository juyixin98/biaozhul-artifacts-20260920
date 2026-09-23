# 跨链消息收件箱(Go + Chi + PostgreSQL)

纯后端的「双模拟链」跨链消息收件箱。两条模拟源链(`chain-a`、`chain-b`)各自
带一个 Ed25519 验证者密钥和若干发送者密钥;收件箱接收带签名与 Merkle 证明的
跨链消息,按**确认深度**放行、按**连续序号前缀**执行,并对重组与冲突证据做出
安全响应。所有哈希、签名、Merkle 运算都是**真实执行**(SHA-256 + Ed25519),
没有任何桩实现。

---

## 1. 它保证什么

消息键 = `(源链 chain_id, 通道 channel_id, 序号 nonce)`,正文以其 SHA-256
摘要参与签名承诺,**正文摘要不可变**。

| 需求 | 实现 |
| --- | --- |
| 只有绑定**已确认源块**的消息可执行 | 块高 `h` 在 `tip - h >= confirmations`(本夹具 = 2)时确认;未确认消息为 `candidate`,执行器只取 `confirmed` |
| 同序号重复到达只执行一次 | `deliveries (chain,channel,nonce)` 唯一约束 + 消息/投递在同一事务提交;相同正文重复提交幂等 |
| 乱序暂存,按连续前缀推进 | 通道维护 `next_nonce` 水位;高位已确认消息先暂存,`process` 从水位逐条推进,遇缺口即停 |
| 确认前源块撤销须取消候选 | 验证者签名的撤销证据只能撤销**当前未确认 tip**;该 tip 下 `candidate` 消息全部置 `cancelled` |
| 已执行消息遇冲突证据 → 冻结通道 + 告警,**不能悄悄回写** | 同键不同正文:若旧消息已执行,通道置 `frozen`、写 `critical` 告警并提交;已执行历史与投递记录保持不变,冲突副本被拒 |
| 终局后的二义性攻击 | 已确认高度出现不同块头 → 不替换历史;受影响通道冻结 + `finalized_equivocation` 严重告警 |
| 真实性 | 块头/撤销由验证者 Ed25519 签名;消息由通道注册发送者对 `(channel,nonce,payloadHash)` 签名;承诺必须通过块消息 Merkle 根的包含证明 |

关键安全不变式:**任何"拒绝"都不会回滚已经产生的告警/冻结副作用**。冲突与
二义性分支先在事务内写入告警与冻结并 **COMMIT**,再把错误返回给调用方。

### 消息生命周期

```
candidate ──块达到确认深度──▶ confirmed ──process 投递成功──▶ executed
    │
    └──所在 tip 被验证者撤销──▶ cancelled(终态)
```

通道状态:`open` → `frozen`(单向,只有人工处置,系统不会自动解冻)。

---

## 2. 密码学与协议(全部真实运算)

- **哈希**:`internal/crypto`,SHA-256,对字符串/哈希序列/整数采用带域分隔与
  长度前缀的规范编码,避免跨类型碰撞。
- **签名**:Ed25519(`crypto/ed25519`)。块头、撤销证据、消息承诺分别在不同
  域下签名;服务端逐条 `ed25519.Verify`。
- **Merkle 树**:`internal/merkle`,二叉 SHA-256 树,奇数节点复制自身上提;
  `BuildProof`/`Verify` 真实计算包含证明(内部节点带 `0x01` 域前缀,区分叶/内
  节点)。单叶块证明为空、多叶块为多级路径,均有测试覆盖。
- **确定性本地夹具**:`internal/fixtures`,密钥由公开种子名
  `SHA256("inbox/fixture/seed/v1"||name)` → Ed25519 派生,任何机器都能复现
  同一批验证者/发送者密钥。**仅限本地模拟,切勿用于真实环境。**

---

## 3. 目录结构

```
cmd/inboxd/          HTTP 服务(迁移、播种双链、后台推进 worker、崩溃点)
cmd/fixturetool/     打印夹具公钥;生成三类示例请求(basic/reorg/conflict)
internal/crypto/     SHA-256 域分离编码、Ed25519 签名/验签、确定性密钥派生
internal/merkle/     Merkle 根与包含证明
internal/fixtures/   双链密钥夹具 + 真实签名块/分叉构造器
internal/store/      pgx 连接池、模式迁移、事务封装(READ COMMITTED + 咨询锁)
internal/core/       领域状态机:头提交/确认、撤销、消息验真、冲突、前缀执行
internal/api/        Chi 路由与 HTTP 处理
internal/wire/       JSON DTO
internal/testsupport/每测试独立 PostgreSQL schema
e2e/                 真实编译并启动 inboxd 子进程的崩溃/重启与 HTTP 场景测试
scripts/             起库脚本与一键验收脚本
examples/            fixturetool 生成的已签名示例请求与 playbook
```

---

## 4. 本地启动

前置:Go 1.22+、Docker(用于 PostgreSQL 16)、`jq`(验收脚本用)。

```bash
# 1) 启动 PostgreSQL(幂等;容器 inbox-pg,宿主端口 55432)
./scripts/start-postgres.sh

# 2) 构建并运行(默认 DSN 见下,可用环境变量覆盖)
go run ./cmd/inboxd
# INBOX_DB / INBOX_ADDR(默认 :8090) / INBOX_INTERVAL(默认 200ms) / INBOX_WORKER
```

服务启动时自动建表迁移并播种 `chain-a`、`chain-b`(确认深度均为 2)。
后台 worker 每 200ms(或收到新数据通知时)推进一次;也可手动
`POST /v1/process` 立即推进。

查看夹具公钥(注册发送者、理解示例时用):

```bash
go run ./cmd/fixturetool keys
```

---

## 5. HTTP API

所有二进制值均为小写 hex。错误体:`{"error":..., "code":...}`。

| 方法与路径 | 说明 |
| --- | --- |
| `GET  /healthz` | 健康检查 |
| `POST /v1/chains/{chainID}/channels` | 开通通道,body:`channel_id`、`sender_pub_hex[]` |
| `GET  /v1/chains/{chainID}/channels/{channelID}` | 通道状态(水位、是否冻结) |
| `POST /v1/headers` | 提交验证者签名块头 `{chain_id,height,parent_hex,msg_root_hex,timestamp,validator_pub_hex,signature_hex}` |
| `POST /v1/revocations` | 提交验证者签名的 tip 撤销证据 `{chain_id,block_hex,validator_pub_hex,signature_hex}` |
| `POST /v1/messages` | 提交消息:发送者签名 + Merkle 证明 + 所在块(见 `examples/*`) |
| `POST /v1/process` | 立即推进所有通道,返回 `{"delivered":N}` |
| `GET  /v1/chains/{chainID}/headers[?all=1]` | 规范头(`all=1` 含已撤销) |
| `GET  /v1/chains/{chainID}/channels/{channelID}/messages` | 消息及状态 |
| `GET  /v1/chains/{chainID}/channels/{channelID}/deliveries` | 已执行投递(恰好一次的事实来源) |
| `GET  /v1/alerts[?chain_id=&severity=&limit=]` | 告警(critical/warning/info) |

典型错误码:`bad_signature`(401)、`bad_proof`(401)、`channel_frozen`(423)、
`revoke_tip_first`(409)、`block_finalized`(422)、`finalized_equivocation`(409)、
`message_conflict`(409)、`message_conflict_channel_frozen`(409)。

### 示例输入

`examples/` 下每个目录都是**已真实签名**的请求 JSON,可直接 `curl --data @文件`
重放,`playbook.txt` 给出带预期结果的有序步骤:

- `examples/basic/`:乱序到达(1、3、2)+ 断档补齐 + 重复幂等;
- `examples/reorg/`:确认前撤销 tip、候选取消、替代块执行;
- `examples/conflict/`:执行后同键不同正文 → 冻结 + critical 告警。

重新生成示例:`go run ./cmd/fixturetool generate examples`。

---

## 6. 验收命令

一键完成:起库 → 隔离验收库 → `go mod verify`/build/vet → **全部自动化测试**
(含真实子进程崩溃/重启)→ 真实 HTTP 跑通断档补齐并校验恰好一次:

```bash
./scripts/accept.sh
```

仅跑测试(需先 `./scripts/start-postgres.sh`):

```bash
go test -count=1 ./...                      # 全量
go test -race -count=1 ./...               # 带竞态检测
go test ./internal/core/ -run TestReorg    # 单场景:确认前重组
go test ./e2e/ -run TestCrashBeforeCommitThenRestart -v   # 真实进程崩溃/重启
```

> 集成/E2E 测试需要 PostgreSQL;不可达时相关测试会 `t.Skip`。可用
> `INBOX_TEST_DSN` 覆盖测试 DSN。每个测试使用独立 schema 并在结束时清理。

---

## 7. 崩溃与重启是怎么测的

`inboxd` 支持仅用于测试的崩溃注入 `INBOX_CRASH=before_deliver@<n>` 或
`before_commit@<n>`(可省略 `@n`)。崩溃点位于投递事务内部:

- `before_deliver`:已拿通道咨询锁、**尚未任何写入**;
- `before_commit`:投递行插入、消息置 `executed`、水位推进都已在事务里
  staged,但**尚未 COMMIT** —— 进程以退出码 99 硬退出。

`e2e/` 会**真正 `go build` 出 inboxd 二进制并启动子进程**:播种后触发
`/process` 使进程在事务中硬崩溃,然后用同一数据库 schema 重启,断言:

1. 崩溃后没有任何半截投递(`before_commit` 的事务随进程死亡整体回滚);
2. 重启后 `/process` 把每个序号恰好投递一次,`attempts` 全为 1;
3. 正常重启后从持久化水位恢复,不会重复执行。

进程内另有 `internal/core/crash_test.go` 用 panic 复现两个崩溃点的事务原子性。

---

## 8. 手工快速体验

```bash
./scripts/start-postgres.sh
go run ./cmd/inboxd            # 终端 A(默认 :8090;可用 INBOX_ADDR=:8090 覆盖)
# 终端 B
go run ./cmd/fixturetool generate /tmp/demo
B=http://localhost:8090; D=/tmp/demo/basic
curl -s -XPOST $B/v1/chains/chain-a/channels -H 'Content-Type: application/json' --data @$D/open-1.json
for f in $D/header-*.json; do curl -s -XPOST $B/v1/headers -H 'Content-Type: application/json' --data @$f; echo; done
curl -s -XPOST $B/v1/messages -H 'Content-Type: application/json' --data @$D/message-1.json
curl -s -XPOST $B/v1/messages -H 'Content-Type: application/json' --data @$D/message-3.json
curl -s -XPOST $B/v1/messages -H 'Content-Type: application/json' --data @$D/message-2.json
curl -s -XPOST $B/v1/process -H 'Content-Type: application/json' --data '{}'
curl -s $B/v1/chains/chain-a/channels/orders/deliveries | jq .
```
