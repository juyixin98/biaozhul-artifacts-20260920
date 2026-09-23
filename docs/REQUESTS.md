# 请求样例（JSON 运行接口）

本项目没有网络端口：**一次“请求”就是一个场景 JSON 文件**，由 `bin/sim -f <file>` 消费，
输出一份运行报告 JSON。节点之间收发的报文也是 JSON 结构（`Envelope`），出现在报告的
`trace` 中。本文给出三类可直接照抄的样例。

- 完整场景见 [`scenarios/`](../scenarios)
- 对应真实运行输出见 [`reports/`](../reports)（由本机构建实际跑出，非手写）

---

## 1. 最小场景：获取锁、提交、释放

```json
{
  "name": "minimal",
  "seed": 1,
  "ttl_ms": 2000,
  "heartbeat_ms": 600,
  "retry_gap_ms": 200,
  "end_ms": 3000,
  "network": { "min_latency_ms": 10, "max_latency_ms": 10, "rules": [] },
  "clients": [ { "id": "A" } ],
  "actions": [
    { "at_ms": 0,    "client": "A", "op": "acquire" },
    { "at_ms": 200,  "client": "A", "op": "submit", "value": "hello", "resource": "R" },
    { "at_ms": 1000, "client": "A", "op": "release" }
  ]
}
```

字段含义：

| 字段 | 含义 |
|---|---|
| `ttl_ms` | 租约长度。未续约则在 `授予时刻 + ttl` 过期 |
| `heartbeat_ms` | 客户端自动续约（心跳）周期，必须 **小于** `ttl_ms` |
| `retry_gap_ms` | `acquire` 收到 `busy` 后重试的间隔 |
| `end_ms` | 仿真终止时刻（逻辑毫秒） |
| `seed` | 确定性随机种子；同种子 + 同输入 ⇒ 逐字节相同的报告 |
| `network.rules` | 脚本化网络故障规则（见下） |

`op` 取值：`acquire` / `renew` / `submit` / `release` / `pause` / `resume` / `note`。

`acquire` 可加 `"retry": true`；`submit` 可加 `"force_submit": true`
（模拟**僵尸旧持有者**：明知租约已失仍用旧围栏号提交，交由资源拒绝）。

---

## 2. 网络故障规则（丢包 / 重复 / 延迟乱序）

规则在 `network.rules` 中按顺序匹配，首条命中生效；可用 `src`/`dst`/`req_type`/
`msg_type`/时间窗 `from`–`until` 精确选择报文。

```json
"network": {
  "min_latency_ms": 10,
  "max_latency_ms": 10,
  "drop_prob": 0.0,
  "duplicate_prob": 0.0,
  "rules": [
    { "type": "drop",      "src": "A", "dst": "L", "req_type": "renew",
      "from": 710, "until": 1430 },
    { "type": "duplicate", "src": "A", "dst": "R", "req_type": "submit",
      "from": 3440, "until": 3470 },
    { "type": "delay",     "src": "A", "dst": "L", "req_type": "renew",
      "delay_ms": 700 }
  ]
}
```

- `drop`：报文永不送达。
- `duplicate`：原件照送，额外再送一份（带 `duplicate_of` 指向原件 id）；
  若指定 `delay_ms`，**原件也被延迟**该时长，副本紧随其后。
- `delay`（`reorder` 同义）：把报文额外滞留 `delay_ms`，制造乱序/迟到。
- 也可不写规则，直接给 `drop_prob` / `duplicate_prob` 做概率故障（由 `seed` 决定）。

`req_type`：`acquire` / `renew` / `release` / `submit`。

---

## 3. 暂停节点（旧持有者继续运行的来源）

```json
{ "at_ms": 500, "client": "A", "op": "pause", "duration_ms": 2500 }
```

暂停期间：客户端定时器（心跳）与所有入站报文被冻结，暂停结束（可提前 `resume`）
后在恢复时刻集中投递。若暂停跨越租约过期点，锁服务会把锁授予他人；恢复后的客户端
仍持有**旧围栏号**，可这样强制提交：

```json
{ "at_ms": 3050, "client": "A", "op": "submit",
  "value": "zombie", "resource": "R", "force_submit": true }
```

---

## 报文样例（trace 中的真实 JSON）

下面这些取自场景 2 的**实际运行**（逻辑毫秒）。

授予锁（围栏号 1）：

```json
{"time_ms": 15, "event": "lock.grant",
 "detail": {"client": "A", "fence": 1, "lease_id": "lease-1", "expire_at": 1015}}
```

A 暂停过久，租约在 t=1438 过期，B 随后拿到**更大的**围栏号 2：

```json
{"time_ms": 1438, "event": "lock.expire",
 "detail": {"holder": "A", "fence": 1, "lease_id": "lease-1", "expire_at": 1438}}
{"time_ms": 1477, "event": "lock.grant",
 "detail": {"client": "B", "fence": 2, "lease_id": "lease-2", "expire_at": 2477}}
```

B 的写入以围栏 2 提交：

```json
{"time_ms": 2212, "event": "resource.commit",
 "detail": {"client": "B", "fence": 2, "req_id": "B:submit:6", "value": "b-write-fence-2"}}
```

A 恢复后用旧围栏号 1 提交 —— 资源以 `stale_fence` 拒绝（`epoch_fence` 是资源已见的最大围栏）：

```json
{"time_ms": 3108, "event": "resource.reject",
 "detail": {"client": "A", "fence": 1, "max_fence": 2,
            "req_id": "A:submit:4", "reason": "stale_fence"}}
```

重复的同一请求（相同 `req_id`）不会二次应用，返回 `duplicate`：

```json
{"event": "lock.renew.dedup", "detail": {"client": "A", "req_id": "A:renew:3", "fence": 1}}
{"event": "resource.submit.dedup", "detail": {"req_id": "A:submit:7", "fence": 1}}
```

---

## 运行

```bash
go build -o bin/sim ./cmd/sim
bin/sim -f scenarios/2-pause-longer-than-ttl.json            # 完整报告（含 trace）
bin/sim -f scenarios/2-pause-longer-than-ttl.json -summary   # 不含逐条 trace
bin/sim -f - < scenarios/1-renewal-lost.json                 # 从标准输入读
```

退出码：`0` 全部不变量通过；`1` 有不变量失败（协议/实现缺陷）；`2` 场景本身非法。
