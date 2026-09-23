# 请求样例

三个场景的 JSON 请求文件就在本目录，可直接作为 `chmig -in` 的输入：

| 文件 | 场景 | 关键观察点 |
|---|---|---|
| `scaleout.json` | 3 节点(权重 1:1:2) → 加入权重 2 的 n4，迁移窗内并发写入，并叠加一次迁移流量黑洞 | 约 1/4 键迁移；黑洞靠重传自愈；COMMIT 后版本与归属正确 |
| `scalein.json` | 4 节点 → 移除 n2，叠加迁移黑洞与并发写 | n2 只有在全部键过屏障、存活属主持有最新版本后才下线 |
| `interrupted.json` | 扩容迁移窗内持续黑洞到模拟结束 | epoch 不提交、迁移量计 0，但已确认写零丢失、最终仍归旧属主 |

运行：

```bash
go run ./cmd/chmig -in examples/scaleout.json     -strict
go run ./cmd/chmig -in examples/scalein.json      -strict
go run ./cmd/chmig -in examples/interrupted.json          # 故意中断，不用 -strict 判失败
```

---

## 样例 1：扩容（scaleout.json，节选）

```json
{
  "seed": 42,
  "vnodesPerWeight": 64,
  "preload": true,
  "lossRate": 0.05,
  "duplicateRate": 0.02,
  "reorderRate": 0.02,
  "minLinkDelay": 1,
  "maxLinkDelay": 3,
  "retryTicks": 20,
  "migrationConcurrency": 8,
  "barrierTicks": 30,
  "drainTicks": 400,
  "initialNodes": [
    {"id": "n1", "weight": 1},
    {"id": "n2", "weight": 1},
    {"id": "n3", "weight": 2}
  ],
  "keys": ["user:0001", "user:0002", "..."],
  "clients": [
    {"id": "c1", "writes": 120, "startTick": 5, "endTick": 220, "readEvery": 4},
    {"id": "c2", "writes": 120, "startTick": 10, "endTick": 240, "readEvery": 5}
  ],
  "operations": [
    {"kind": "scaleOut", "tick": 60, "add": [{"id": "n4", "weight": 2}]},
    {"kind": "interrupt", "tick": 90, "endTick": 130}
  ]
}
```

响应（节选）：

```json
{
  "topologyEpochs": [
    {"version": 0, "nodes": ["n1","n2","n3"], "beganAt": 0,   "commitAt": 0},
    {"version": 1, "nodes": ["n1","n2","n3","n4"], "beganAt": 60, "commitAt": 166}
  ],
  "migration": {
    "keysMoved": 5, "keysSwitched": 5, "epochsCommitted": 1,
    "recordVersionsTransferred": 11,
    "bytesTransferred": 82, "bytesMoved": 36, "overheadBytes": 46,
    "barriersPassed": 5,
    "movements": [{"key": "user:0021", "from": "n2", "to": "n4", "ringVersion": 1,
                   "committed": true, "beganAt": 63, "switchedAt": 136}]
  },
  "verifications": {
    "ownership": {"pass": true},
    "version": {"pass": true},
    "confirmedWritesSurvive": {"pass": true},
    "removalSafety": {"pass": true},
    "noStaleReadAccepted": {"pass": true}
  }
}
```

`recordVersionsTransferred(11) > keysMoved(5)`：5 个键各传一份快照，另含迁移窗口内
被前转发的 6 个并发写版本；`overheadBytes` 即这部分额外迁移量。

---

## 样例 2：缩容 / 节点移除（scalein.json）

与扩容相同，区别在操作：

```json
"initialNodes": [
  {"id": "n1", "weight": 1}, {"id": "n2", "weight": 1},
  {"id": "n3", "weight": 2}, {"id": "n4", "weight": 2}
],
"operations": [
  {"kind": "scaleIn", "tick": 80, "remove": ["n2"]},
  {"kind": "interrupt", "tick": 110, "endTick": 150}
]
```

结果要点：

```json
{
  "removedNodes": ["n2"],
  "topologyEpochs": [
    {"version": 1, "nodes": ["n1","n3","n4"], "beganAt": 80, "commitAt": 176}
  ],
  "migration": {"keysMoved": 7, "epochsCommitted": 1},
  "verifications": { "removalSafety": {"pass": true}, "...": "..." }
}
```

n2 原属主的键全部迁到 n3/n4；移除安全闸门在 COMMIT 前确认 n2 不再持有任何键的最新
版本，之后才发 DECOMMISSION，`removedNodes` 才出现 n2。

---

## 样例 3：迁移中断（interrupted.json）

```json
"stopTick": 180,
"operations": [
  {"kind": "scaleOut", "tick": 50, "add": [{"id": "n4", "weight": 2}]},
  {"kind": "interrupt", "tick": 70, "endTick": 175}
]
```

黑洞几乎覆盖整个迁移窗，COMMIT 无法在 `stopTick` 前完成。结果：

```json
{
  "topologyEpochs": [
    {"version": 1, "beganAt": 50, "commitAt": 0}
  ],
  "migration": {
    "keysMoved": 0, "keysSwitched": 0, "epochsCommitted": 0,
    "recordVersionsTransferred": 0, "bytesTransferred": 0,
    "movements": []
  },
  "stats": {"writesConfirmed": 170, "writesFailed": 0},
  "verifications": {
    "ownership": {"pass": true},
    "version": {"pass": true},
    "confirmedWritesSurvive": {"pass": true},
    "removalSafety": {"pass": true},
    "noStaleReadAccepted": {"pass": true}
  }
}
```

迁移未提交，新环不作为权威；屏障后未决的写**从未被确认**，而全部已确认写仍在旧属主
（最终环）上。该例的 `writesIssued - writesConfirmed` 即因中断而挂起、正确地不计为
成功也不丢失的写。把黑洞 `endTick` 提前（例如 110）并加大 `drainTicks`，同一协议会
靠重传恢复并提交——`scaleout.json`/`scalein.json` 正是该恢复路径的验收。
