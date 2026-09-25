# HTTP API

本地接口，基于 Go 标准库 `net/http`（Go 1.22 的方法/路径通配路由）。
默认监听 `:8080`，可用 `-addr` 修改。

所有请求/响应均为 `application/json; charset=utf-8`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活检查 |
| GET | `/api/scenarios` | 列出内置场景（含资源、选项、任务概要） |
| GET | `/api/scenarios/{name}` | 直接运行内置场景并返回完整结果 |
| POST | `/api/simulate` | 提交自定义 Spec 运行 |

状态码：`200` 成功；`400` 请求体/语义非法（错误信息在 `error` 字段）；
`404` 未知场景。请求体上限 1 MiB，未知 JSON 字段会被拒绝。

## GET /healthz

```json
{"status": "ok"}
```

## GET /api/scenarios

返回：

```json
{
  "scenarios": [
    {
      "name": "deadlock-ab-ba",
      "label": "ab-ba-deadlock",
      "resources": ["R1", "R2"],
      "options": {"priorityInheritance": true, "deadlockDetection": true},
      "tasks": [
        {"id": "T1", "arrival": 0, "priority": 3, "actionCount": 6},
        {"id": "T2", "arrival": 1, "priority": 4, "actionCount": 5}
      ]
    }
  ]
}
```

内置名称：`classic-inversion-no-pi`、`classic-inversion-pi`、
`three-level-inheritance`、`multi-lock-r1-first`、`multi-lock-r2-first`、
`deadlock-ab-ba`、`inherit-restore`。

## POST /api/simulate

请求体即 Spec：

```json
{
  "name": "string，可省略",
  "resources": ["R"],
  "options": {
    "priorityInheritance": true,
    "deadlockDetection": true
  },
  "tasks": [
    {
      "id": "High",
      "arrival": 0,
      "priority": 3,
      "program": {"actions": [{"acquire": "R"}, {"cpu": 1}, {"release": "R"}]}
    }
  ]
}
```

字段约束：

- `tasks[].id` 非空且唯一；`priority` 数值越大优先级越高；
- `program.actions[]` 每项恰有一个字段：`cpu`(非负整数) / `acquire` /
  `release`（锁名非空）；
- 所有 `acquire/release` 引用的锁必须出现在 `resources` 中。

## 响应：Result

```json
{
  "name": "...",
  "options": {"priorityInheritance": true, "deadlockDetection": true},
  "finishTime": 12,
  "deadlock": {"cycle": ["T1", "T2", "T1"], "at": 1},
  "error": "",
  "events": [
    {"seq": 0, "time": 0, "kind": "task_dispatch",
     "task": "Low", "detail": {"basePriority": 1, "effectivePriority": 1}}
  ],
  "summary": {
    "makespanTicks": 12,
    "inversionTicks": 0,
    "blockedTicks": {"High": 7},
    "completedAt": {"Low": 8, "High": 10, "Urgent": 12}
  },
  "tasks": [
    {"id": "Low", "state": "done", "basePriority": 1,
     "effectivePriority": 1, "donors": [], "arrival": 0,
     "completedAt": 8, "blockedTicks": 0}
  ],
  "locks": [
    {"id": "R1", "owner": "", "waiters": []}
  ]
}
```

说明：

- `deadlock` 仅在检出且开启检测时出现；关闭检测时环以非空 `error` 终止；
- `state`：`waiting / ready / running / blocked / done`；
- `inversionTicks`：教科书“无界反转”tick 数——存在比运行者基础优先级更高
  的就绪等待者，且其阻塞链根持有者并非当前运行者（即被无关中等任务拖延）；
- `events[].kind` 全集：

| kind | 含义 |
|---|---|
| `task_arrive` | 到达时刻进入就绪 |
| `task_dispatch` | 被调度上处理器 |
| `task_preempted` | 被更高有效优先级任务抢占 |
| `task_block` | P 操作失败而阻塞（detail.owner 为持有者） |
| `task_wakeup` | 阻塞者被直接授予锁而唤醒 |
| `task_exit` | 程序完成 |
| `lock_acquire` | 自由锁被取得 |
| `lock_release` | 释放锁 |
| `lock_grant` | 锁直接移交给等待者（detail.from / waitersRemaining） |
| `priority_change` | 有效优先级变化（old/new/donors/reason） |
| `tick` | 完成一个处理器 tick（burstLeft / effectivePriority） |
| `idle_jump` | 处理器空闲，时间跳到下一到达点 |
| `deadlock` | 等待图成环（cycle/edges/at） |
| `program_error` | 非法运行时操作（如释放非自有锁、重入） |

## 示例请求

见仓库 `examples/`：

```bash
curl -s -X POST http://127.0.0.1:8080/api/simulate \
  -H 'Content-Type: application/json' \
  --data @examples/classic-inversion-no-pi.json
```
