# 请求样例

以下样例驱动本地 HTTP 服务完整复现三条竞态/语义线。先启动服务：

```bash
go run ./cmd/demo serve --addr :8080
```

虚拟时钟起点固定为 `1970-01-01T00:00:00Z`，只有显式 advance 才会前进。
另一个终端执行下面的命令。建议安装 `jq` 以便阅读输出。

---

## 0. 观察初始状态

```bash
curl -s localhost:8080/state | jq
```

要点：`state: "closed"`、`generation: 0`、`window_samples: 0`。

---

## 1. 旧失败迟到（late failure from old generation）

```bash
# 重置到干净状态
curl -s -X POST localhost:8080/reset | jq -c .state

# 1.1 让上游挂起，发起一个“永远在飞”的旧代调用（异步，立即返回 id）
curl -s -X POST localhost:8080/upstream/mode -d '{"mode":"hang"}' | jq -c .
curl -s -X POST localhost:8080/call/async | jq -c .          # -> {"id":1}
curl -s localhost:8080/upstream/pending | jq -c .            # -> {"pending":[1]}

# 1.2 上游对其他流量开始失败；5 次失败把断路器打到 open
curl -s -X POST localhost:8080/upstream/mode -d '{"mode":"fail"}' | jq -c .
for i in 1 2 3 4 5; do curl -s -X POST localhost:8080/call | jq -c '.result.outcome'; done
curl -s localhost:8080/state | jq -c '{state,generation,window_failures:.window_failures}'

# 1.3 推进虚拟冷却 5 秒，上游恢复，两次探测成功 -> closed（gen3）
curl -s -X POST localhost:8080/clock/advance -d '{"ms":5000}' | jq -c .
curl -s -X POST localhost:8080/upstream/mode -d '{"mode":"ok"}' | jq -c .
curl -s -X POST localhost:8080/call | jq -c '{state:.snapshot.state,probe:.result.probe,gen:.result.generation}'
curl -s -X POST localhost:8080/call | jq -c '{state:.snapshot.state,gen:.snapshot.generation}'

# 1.4 关键一步：gen0 的旧调用直到现在才失败
curl -s -X POST localhost:8080/upstream/release -d '{"mode":"fail","id":1}' | jq -c .
curl -s localhost:8080/calls | jq -c '.calls[] | select(.id==1) | {done,outcome:.result.outcome,gen:.result.generation}'

# 1.5 验收：断路器仍 closed/gen3，失败计数没有增加，stale_results=1
curl -s localhost:8080/state | jq -c '{state,generation,failures:.counters.failures,stale:.counters.stale_results}'
# 期望: {"state":"closed","generation":3,"failures":5,"stale":1}
```

---

## 2. 半开探测名额竞争（probe competition）

```bash
curl -s -X POST localhost:8080/reset | jq -c .state

# 2.1 跳闸并过完冷却
curl -s -X POST localhost:8080/upstream/mode -d '{"mode":"fail"}' | jq -c .
for i in 1 2 3 4 5; do curl -s -X POST localhost:8080/call >/dev/null; done
curl -s -X POST localhost:8080/clock/advance -d '{"ms":5000}' | jq -c .

# 2.2 上游挂起；占满 3 个探测名额（异步）
curl -s -X POST localhost:8080/upstream/mode -d '{"mode":"hang"}' | jq -c .
for i in 1 2 3; do curl -s -X POST localhost:8080/call/async | jq -c .; done
curl -s localhost:8080/state | jq -c '{state,inflight:.probes_in_flight}'
# -> {"state":"half_open","inflight":3}

# 2.3 第 4 个竞争者：不排队，快速失败
curl -s -X POST localhost:8080/call | jq -c .rejected
# -> "half-open probe slots exhausted"
curl -s localhost:8080/state | jq -c '{inflight:.probes_in_flight,rejected:.counters.probes_rejected}'

# 2.4 释放一个探测为成功 -> 名额被释放（注意默认 required=2，
#     一个成功后仍在 half_open），第 4 个竞争者此时可以进入
curl -s -X POST localhost:8080/upstream/release -d '{"mode":"ok"}' | jq -c .released
sleep 0.2
curl -s localhost:8080/state | jq -c '{state,inflight:.probes_in_flight,streak:.consecutive_successes}'

# 2.5 清理剩余挂起调用（全部成功），观察恢复
curl -s -X POST localhost:8080/upstream/release -d '{"mode":"ok","all":true}' | jq -c .released
sleep 0.2
curl -s localhost:8080/state | jq -c '{state,generation,inflight:.probes_in_flight}'
```

> 提示：`/call` 在 hang 模式下会阻塞直到释放，所以占名额请用
> `/call/async`；观察名额用 `/state`。

---

## 3. 完整恢复生命周期 + 冷却期拒绝

```bash
curl -s -X POST localhost:8080/reset | jq -c .state

curl -s -X POST localhost:8080/upstream/mode -d '{"mode":"fail"}' | jq -c .
for i in 1 2 3 4 5; do curl -s -X POST localhost:8080/call >/dev/null; done
curl -s localhost:8080/state | jq -c '{state,generation,failures:.counters.failures}'

# open 期间调用被直接拒绝（不触达上游）
for i in 1 2 3; do curl -s -X POST localhost:8080/call | jq -c .rejected; done

# 冷却差 1 秒时仍拒绝
curl -s -X POST localhost:8080/clock/advance -d '{"ms":4000}' >/dev/null
curl -s -X POST localhost:8080/call | jq -c .rejected

# 到点：下一次调用惰性进入 half_open 并作为探测
curl -s -X POST localhost:8080/clock/advance -d '{"ms":1000}' >/dev/null
curl -s -X POST localhost:8080/upstream/mode -d '{"mode":"ok"}' | jq -c .
curl -s -X POST localhost:8080/call | jq -c '{state:.snapshot.state,probe:.result.probe}'
curl -s -X POST localhost:8080/call | jq -c '{state:.snapshot.state,generation:.snapshot.generation}'

# 新窗口干净，健康流量恢复
for i in 1 2 3; do curl -s -X POST localhost:8080/call >/dev/null; done
curl -s localhost:8080/state | jq -c '{state,window:.window_samples,failures:.window_failures,ratio:.failure_ratio}'
```

---

## 4. 取消不是失败（canceled ≠ failure）

### 4a. closed 状态下取消

```bash
curl -s -X POST localhost:8080/reset | jq -c .state
curl -s -X POST localhost:8080/upstream/mode -d '{"mode":"hang"}' | jq -c .
ID=$(curl -s -X POST localhost:8080/call/async | jq .id)
curl -s -X POST localhost:8080/call/$ID/cancel | jq -c .
sleep 0.2
curl -s localhost:8080/calls | jq -c --arg id "$ID" '.calls[] | select(.id==($id|tonumber)) | .result.outcome'
# -> "canceled"

# 上游事后把这个已放弃的调用做成失败，也不改变任何计数
curl -s -X POST localhost:8080/upstream/release -d '{"mode":"fail","all":true}' | jq -c .
curl -s localhost:8080/state | jq -c '{state,failures:.counters.failures,canceled:.counters.canceled}'
# -> {"state":"closed","failures":0,"canceled":1}
```

### 4b. half_open 探测被取消：释放名额、不改连续成功数

```bash
curl -s -X POST localhost:8080/reset | jq -c .state
curl -s -X POST localhost:8080/upstream/mode -d '{"mode":"fail"}' >/dev/null
for i in 1 2 3 4 5; do curl -s -X POST localhost:8080/call >/dev/null; done
curl -s -X POST localhost:8080/clock/advance -d '{"ms":5000}' >/dev/null
curl -s -X POST localhost:8080/upstream/mode -d '{"mode":"hang"}' >/dev/null

# 第一个调用进入 half_open 作为探测并挂起
PID=$(curl -s -X POST localhost:8080/call/async | jq .id)
curl -s localhost:8080/state | jq -c '{state,inflight:.probes_in_flight}'
# -> {"state":"half_open","inflight":1}

# 取消该探测
curl -s -X POST localhost:8080/call/$PID/cancel | jq -c .
sleep 0.2
curl -s localhost:8080/state | jq -c '{state,inflight:.probes_in_flight,streak:.consecutive_successes,failures:.counters.failures}'
# -> {"state":"half_open","inflight":0,"streak":0,"failures":5}

# 名额已释放：两个成功探测完成恢复
curl -s -X POST localhost:8080/upstream/mode -d '{"mode":"ok"}' >/dev/null
curl -s -X POST localhost:8080/call >/dev/null
curl -s -X POST localhost:8080/call | jq -c '{state:.snapshot.state,failures:.snapshot.counters.failures,canceled:.snapshot.counters.canceled}'
# -> {"state":"closed","failures":5,"canceled":2}
```

---

## 5. 虚拟超时（timeout 算 failure，不是 canceled）

```bash
curl -s -X POST localhost:8080/reset | jq -c .state
curl -s -X POST localhost:8080/client/config -d '{"timeout_ms":100}' | jq -c .timeout
curl -s -X POST localhost:8080/upstream/mode -d '{"mode":"hang"}' >/dev/null

# 异步发起，推进虚拟时间 100ms，客户端因虚拟超时判定失败
ID=$(curl -s -X POST localhost:8080/call/async | jq .id)
curl -s -X POST localhost:8080/clock/advance -d '{"ms":100}' | jq -c .timers_fired
sleep 0.2
curl -s localhost:8080/calls | jq -c --arg id "$ID" '.calls[] | select(.id==($id|tonumber)) | {outcome:.result.outcome,reason:.result.reason,elapsed:.result.elapsed_virtual}'
# -> {"outcome":"failure","reason":"timeout","elapsed":100000000}

curl -s localhost:8080/state | jq -c '{failures:.counters.failures,canceled:.counters.canceled}'
# -> {"failures":1,"canceled":0}

# 清理仍挂在假服务端的调用
curl -s -X POST localhost:8080/upstream/release -d '{"mode":"fail","all":true}' >/dev/null
```

---

## 6. 传输故障注入

```bash
curl -s -X POST localhost:8080/reset >/dev/null
# 接下来 2 次调用注入传输错误（请求不到达上游）
curl -s -X POST localhost:8080/client/config -d '{"fail_next_n":2}' >/dev/null
for i in 1 2; do curl -s -X POST localhost:8080/call | jq -c .result.reason; done
# -> "injected_transport_error" x2
curl -s -X POST localhost:8080/call | jq -c .result.outcome   # -> "success"

# 每 3 次失败一次
curl -s -X POST localhost:8080/client/config -d '{"fail_every_nth":3}' >/dev/null
for i in 1 2 3 4 5 6; do curl -s -X POST localhost:8080/call | jq -c .result.outcome; done
```
