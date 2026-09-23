# HTTP 接口与请求样例

所有接口仅监听本地；请求体与响应体均为 JSON。时间字段单位为**单调纳秒**。

- 成功扣减：`200 OK`
- 立即请求被限流：`429 Too Many Requests`
- 请求超过桶容量 / 非法令牌数：`422 Unprocessable Entity`
- 请求体或参数非法：`400 Bad Request`
- 调度已接收：`202 Accepted`

## 配置对象（Config）

```json
{
  "rate": { "rate_num": 2, "rate_den_ns": 1000000000 },
  "capacity": 5,
  "initial_tokens": 2
}
```

- `rate.rate_num` / `rate.rate_den_ns`：每 `rate_den_ns` 纳秒产出 `rate_num` 个整数令牌。
  `rate_num=0` 表示**停止补充**（存量可继续被消费）。
- `capacity`：桶容量（突发上限），范围 `[1, 1e9]`。
- `initial_tokens`：可选，仅在建桶时生效，范围 `[0, capacity]`；缺省为满桶。

约束：`rate_num ∈ [0,1e9]`；正速率要求 `rate_den_ns ∈ [1, 31536000s(1年纳秒)]`。

---

## GET /healthz

```bash
curl -s 127.0.0.1:18080/healthz
# {"status":"ok"}
```

## GET /state

返回全局与每个租户在当前时刻的快照。

```json
{
  "at_ns": 1790170558728160908,
  "global": {
    "rate_num": 0, "rate_den_ns": 0, "capacity": 100,
    "available": 100, "frac_scaled_ns": 0,
    "anchor_ns": 1790170558728160908, "committed": false
  },
  "tenants": {
    "acme": {
      "rate_num": 0, "rate_den_ns": 0, "capacity": 4,
      "available": 2, "frac_scaled_ns": 0,
      "anchor_ns": 1790170558728160908, "committed": false
    }
  }
}
```

字段含义：

- `available`：整令牌存量；精确存量 = `available + frac_scaled_ns/1e9`。
- `anchor_ns`：记账锚点。`anchor_ns > at_ns` 且 `committed=true` 表示已有
  未来预留把“将在锚点前补充出的令牌”提前锁定，这些令牌不可再被立即请求消费。

## POST /request —— 立即两层扣减

```bash
curl -s -X POST 127.0.0.1:18080/request \
  -H 'Content-Type: application/json' \
  -d '{"tenant":"acme","tokens":1}'
```

成功（200）：

```json
{
  "allowed": true,
  "wait_ns": 0,
  "state": { "...": "扣减后的 /state" }
}
```

因租户层不足而拒绝（429）。关键不变量：全局层若仍有令牌也**不会被扣掉**
（响应 `state.global.available` 保持不变）：

```json
{
  "allowed": false,
  "reason": "insufficient_tokens",
  "wait_ns": 1000000000,
  "state": { "...": "两层均未被消费的快照" }
}
```

超过桶容量（422，两层零消费）：

```json
{ "allowed": false, "reason": "tokens_exceed_capacity", "wait_ns": 0, "state": {} }
```

> `wait_ns` 表示“要等到两层都够还需多少纳秒”（取两层较大者）；
> 若其中一层是停止补充且存量不足（永远无法满足），`wait_ns=0`。

## POST /schedule —— 预留并延迟执行

```bash
curl -s -X POST 127.0.0.1:18080/schedule \
  -H 'Content-Type: application/json' \
  -d '{"tenant":"acme","name":"job-A","tokens":1}'
```

已接收（202）。令牌在**预留时刻**即从两层原子锁定，作业在 `ready_at_ns`
（单调时刻）由内置执行器触发：

```json
{
  "accepted": true,
  "wait_ns": 985136666,
  "job": {
    "id": "job-1", "tenant": "acme", "tokens": 1, "name": "job-A",
    "status": "queued",
    "ready_at_ns": 1790170560853969632,
    "wait_ns": 985136666
  }
}
```

多个预留按“下一张可用令牌时刻”排队：第 1、2 个作业会分别得到相隔一个补充
周期的 `ready_at_ns`，不会超额承诺。

超过容量（422，拒绝且零消费）：

```json
{
  "accepted": false,
  "wait_ns": 0,
  "reason": "tokens_exceed_capacity",
  "job": { "id": "job-3", "status": "rejected", "tokens": 99, "...": "" }
}
```

等待超过 `-horizon` 时返回 429（`reason=insufficient_tokens`），同样零消费。

## GET /jobs、GET /jobs/{id}

```bash
curl -s 127.0.0.1:18080/jobs
curl -s 127.0.0.1:18080/jobs/job-1
```

`status` 取值：`queued` → `running` → `succeeded` / `failed`；
无法预留时为 `rejected`。

## PUT /config/global

```bash
curl -s -X PUT 127.0.0.1:18080/config/global \
  -H 'Content-Type: application/json' \
  -d '{"rate":{"rate_num":10,"rate_den_ns":1000000000},"capacity":50}'
```

返回替换后的 `/state`（200）。语义保证：

- 提高 `capacity` **不会**凭空增加存量；
- 降低 `capacity` 会把超出的存量钳制掉（销毁，不新增）；
- 切换 `rate` 前先按旧速率把已累积进度结算，再保守换算亚令牌余数
  （向下取整：宁可损失一个舍入零头，也不凭空造令牌）；
- `rate_num` 从 0 恢复为正时，锚点重置为当前时刻——停止期间**不补涨**。

## PUT /config/tenants/{id}

租户不存在则创建，存在则替换；语义与全局一致。

```bash
curl -s -X PUT 127.0.0.1:18080/config/tenants/beta \
  -H 'Content-Type: application/json' \
  -d '{"rate":{"rate_num":0,"rate_den_ns":0},"capacity":1}'
```

非法配置返回 400，例如：

```json
{"error":"budget: invalid configuration: capacity must be in [1,1000000000]"}
```

## GET /events

返回进程内累积的结构化事件（按全局单调序号 `seq` 排序）：

```json
{ "events": [ { "seq": 3, "at_ns": 1790170559000000000,
                 "kind": "granted",
                 "detail": { "scope": "global", "tenant": "acme",
                             "tokens": 1, "remaining": 9 } } ] }
```

事件种类：

| kind             | 触发时机                                   |
|------------------|--------------------------------------------|
| `tenant_created` | 租户桶首次创建                             |
| `granted`        | 立即请求在某一层成功扣减（每层一条）       |
| `denied`         | 请求被拒（每层一条），带 `reason`/`wait_ns`|
| `reserved`       | 调度在某一层完成未来预留（每层一条）       |
| `config_changed` | rate/capacity 替换，含 `before`/`after`    |
| `job_queued`     | 调度已接收                                 |
| `job_executed`   | 作业执行结束                               |
| `job_rejected`   | 调度无法预留                               |

`scope` 为 `global` / `tenant` / `system`。使用 `-events` 启动时，同样的事件
还会以一行一个 JSON 对象（JSON Lines）追加到指定文件。
