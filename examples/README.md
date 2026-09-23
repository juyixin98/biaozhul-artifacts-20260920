# 请求样例说明

本目录每个 `.json` 都是一个可直接喂给 CLI 的完整请求。运行方式：

```bash
go build -o ../bin/twopcsim ../cmd/twopcsim
../bin/twopcsim -scenario 01-happy-path.json
```

## 最小请求（干净网络，正常提交）

见 `01-happy-path.json`：没有 `crashes`，所有概率为 0，事务在 tick 5 由协调者开启。

## 有损网络请求

见 `02-lossy-network.json`：`lossRate/duplicateRate/reorderRate` 取值 [0,1]，
由 `seed` 决定每条消息的具体随机结果。改变 `seed` 会得到不同但确定的丢包序列。

## 崩溃恢复请求（两种写法二选一）

1. **协议钩子（推荐，精确到协议位置）**：
   ```json
   "crashes": [
     { "nodeId": "coordinator",
       "hook": "coordinator.commit.appended",
       "restartTick": 120 }
   ]
   ```
   - 省略 `restartTick`（或写 0）= 本轮永久不可用（用于演示阻塞，见 05、06）。
2. **绝对时刻**：
   ```json
   "crashes": [ { "nodeId": "coordinator", "atTick": 9 } ]
   ```

可对同一节点配置多条规则；`occurrence` 控制钩子第几次命中才崩溃，
`txnId` 把钩子限定到某一事务（多事务场景）。

## 本地否决请求

在 transaction 上设置 `"voteNo": ["participant-2"]`，该参与者第一阶段投
VOTE_ABORT，协调者必须全局中止（见 `09-vote-no-abort.json`）。

## 跨进程恢复请求（演示阻塞可解除）

```bash
# 进程 1：协调者提交点后永久宕机，事务阻塞，WAL 落盘到 var/demo
sed 's#"fresh": true,#"fresh": true, "dataDir": "var/demo",#' \
    05-coord-down-forever-blocked.json > /tmp/run1.json
../bin/twopcsim -scenario /tmp/run1.json -out /tmp/run1-report.json   # => blocked

# 进程 2：同一 dataDir、fresh:false、不再注入崩溃；协调者恢复并完成提交
cat > /tmp/run2.json <<'JSON'
{
  "name": "resume", "seed": 1, "maxTick": 300,
  "dataDir": "var/demo", "fresh": false,
  "network": { "minDelay": 1, "maxDelay": 3 },
  "timings": { "voteTimeout": 60, "resend": 12, "query": 8 }
}
JSON
../bin/twopcsim -scenario /tmp/run2.json -out /tmp/run2-report.json   # => committed (3/3)
```

`accept.sh` 第 7 步会自动执行等价的两进程检查。
