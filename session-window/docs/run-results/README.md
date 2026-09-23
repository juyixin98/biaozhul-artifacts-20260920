# 实测记录（2026-09-23，本机真实执行）

环境：Linux x86_64，Eclipse Temurin JDK `17.0.20.1+1`，零第三方依赖。

## 自动化测试：`scripts/test.sh`

原始输出见 [`test-output.txt`](test-output.txt)。结果：

```
SessionAggregator unit tests: 18/18 passed
Json unit tests:               7/7 passed
HTTP integration tests:       10/10 passed
ALL SUITES GREEN        (退出码 0)
```

覆盖：纯切分、验收桥接（0/20/10，gap=10）、撤回不重复计数、changelog 净值、
重启后会话 ID/版本稳定、水位边界（`==` 保留 / `<` 拒收 / lateness=0）、
gap 边界、多 key、幂等去重、eventId 冲突 409、三路桥接、JSON 往返与坏输入、
HTTP 端到端、批量、坏 JSON 400、changelog 分页、真实停服重启恢复。

## 端到端 HTTP 示例：`scripts/examples.sh`

原始输出见 [`example-output.txt`](example-output.txt)。关键点（真实抓取）：

1. `e0@0` → 新建 `k@0 v1`；`e20@20` → 新建 `k@20 v1`（20-0=20 > gap 10）。
2. 迟到 `e10@10`（10-0=10、20-10=10，均 `<= gap`）→ 输出 3 条变更：
   `RETRACT k@20 v1`、`RETRACT k@0 v1`、`UPSERT k@0 v2 [e0,e10,e20]`。
3. 桥接后 `/sessions` 仅 1 个会话 `k@0 v2`，start=0/end=20。
4. `/accounting`：`materializedEventRows=3`、UPSERT 行数−RETRACT 行数净值=3、
   `noDoubleCount=true`。
5. 幂等重投 `e10` → `200 duplicate:true` 且无新变更；同 id 改时间戳 → `409`。
6. 水位推进到 90 后，`t=5` → `422` 拒收（`/rejected` 恰 1 条）；
   `t=90 == watermark` 被接收并把 `k@100` 桥接进新会话 `k@90`。
7. 批量在另一 key 上逐事件产生正确的版本演进。

## 真实停服重启恢复（同端口、同数据目录）

- 停服前持久化日志：`data/events.log` 仅含 8 条**已接收**事件（拒收不写）。
- `kill` 停进程 → 同目录同端口重新 `scripts/run.sh`，启动日志 `recovered 8 events`。
- 重启后 `/sessions` 与停服前逐项一致：

```
('grp@10', version 2, ['b1','b2'])
('grp@50', version 1, ['b3'])
('k@0',   version 2, ['e0','e10','e20'])
('k@90',  version 1, ['edge','hi'])
noDoubleCount=True  materializedEventRows=8  watermark=90
```

- `/rejected` 在重启后计数为 0：拒收事件不参与恢复，符合“仅持久化已接收事件”
  的设计（水位由重放确定性重建）。
