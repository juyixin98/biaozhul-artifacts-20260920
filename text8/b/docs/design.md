# 设计说明

## 数据模型

- `templates`：模板（key 唯一），`current_version_id/number` 是"当前版本指针"。
- `template_versions`：版本行，`(template_id, version)` 唯一；`definition`(JSONB)
  在 `published` 后没有任何修改入口；`checksum` 为定义的 SHA-256。
- `instances`：实例创建时写入 `version_id/version_number`，**永不更新**。
  模板当前指针如何变化（发布新版、回滚）都不影响运行中的实例。
- `node_activities`：实例在某个审批节点的一次驻留。条件节点是瞬时的（同步穿过），
  不产生 activity。`deadline` 驱动超时；`escalated` 保证升级只做一次。
- `tasks`：待办，挂在 activity 上，`(activity_id, assignee)` 唯一。
- `audit_logs`：每一次有效状态转换一条记录，与状态/待办在**同一事务**提交。
- `request_ledger`：幂等台账，唯一键 `request_id`，存首次成功响应快照。

状态：实例 `running / approved / rejected / withdrawn`；
待办 `pending / approved / rejected / closed / withdrawn / timeout_closed`。

## 版本不变性与隔离

- 变更是"追加新版本"，不是原地修改；回滚是移动指针。因此旧版本定义物理上一直存在，
  旧实例的运行时定义只从自己绑定的版本读取（`get_instance_definition`）。
- 发起实例时 `version` 缺省取当前指针；显式指定则必须是已发布版本。
- 每次审批/撤回带 `expected_version`，与实例实际版本不符直接 409，
  让持有旧界面的客户端立刻感知。

## 发布前校验（validator.py）

1. 恰好 1 个 start、至少 1 个 end、节点 id 唯一；
2. 引用完整性：所有 `next / branches.next / default / on_reject` 必须存在且不能指向 start；
   end 不能有出边；
3. 审批配置：审批人不重复；超时时间与动作必须成对；升级目标非空、不与原审批人重叠；
4. 条件表达式全部通过 AST 白名单静态校验；条件必须有 default 或恒真分支；
5. 从 start DFS 计算可达集，不可达节点拒绝；
6. 在可达子图上 DFS 三色标记检测环路。

## 受限表达式（expressions.py）

`ast.parse(mode="eval")` 后做**白名单遍历**，再递归求值，全程不碰 `eval/exec/compile`。
允许的节点仅限字面量（bool/int/float/str/None）、Name、算术/布尔/比较/一元运算、
三元、list/tuple；比较含 `in/not in/is/is not`。函数调用、属性、下标、lambda、
推导式、f-string、海象、星号等一律拒绝；`**` 指数设上界防大整数 DoS。
变量只取自实例 context，未知变量在运行时抛错（该次审批 409，不产生状态变更）。

## 状态机推进（engine.py）

`start → 审批/条件/end`。进入审批节点：建 pending activity（含 deadline）+ 每人一条待办；
进入条件节点：按顺序求值分支，命中即走，否则走 default，条件链路同步穿透直到
下一个审批/end；进入 end：写终态与 `complete` 审计。

- **全签**：每张票锁住实例→活动→全部待办；全部 APPROVED 才推进；
- **任签**：第一张 APPROVED 即推进，其余待办置 `closed` 并写 `close_tasks`；
- **明确拒绝**（全签/任签一致）：当前待办置 REJECTED，其余 pending 待办置 `closed`，
  activity 置 REJECTED，记录拒绝原因；`on_reject` 有目标则进入该目标（通常是 end-rejected，
  也允许指向其他节点形成"驳回修改"类流程），无目标则直接终止 rejected。

撤回：提交人在 running 时可撤回；锁实例后关闭全部 pending activity 的待办，
置 `withdrawn` 终态。

## 并发控制：只有一次有效转换

所有写路径的加锁顺序统一为 **instance 行 → node_activity 行 → task 行**：

- 审批：`SELECT … FOR UPDATE` 先锁实例，再锁该 activity，再锁待办集合。
  并发的两张票因此严格串行：后到的事务看到 activity 已终结/自己的待办已非 pending，
  得到 409，不产生第二条审计。
- 撤回、超时扫描同样先锁实例，天然互斥。
- 超时扫描选取实例时使用 `FOR UPDATE SKIP LOCKED`，多实例并行、单实例不重入，
  扫描器实例可以多副本运行（本项目只起一个线程，但语义已支持）。

幂等：业务事务内先在 SAVEPOINT 中插入 `request_ledger` 占位行（唯一键冲突→回放），
再执行业务，成功后回填响应并随事务提交；业务失败则整个事务回滚——占位行、状态、
待办、审计全部消失，所以冲突**不写历史**，同一 request_id 修正请求后可重试。
并发的相同 request_id 由唯一约束兜底，一个赢家提交，另一个回放结果。

## 超时升级（timeouts.py / sweeper.py）

- 扫描事务：`join instances` 选出 running + pending + deadline 到期的实例，
  `SKIP LOCKED` 取锁，按 instance id 排序输出；
- 每个实例一个独立事务重新锁实例并处理全部到期 activity：
  - escalate：旧待办 `timeout_closed` + 审计 `escalate` + 为升级目标建待办；
    `escalated=true` 且 deadline 清空 → 只升级一次，升级待办不再超时；
  - auto_approve/auto_reject：系统身份走与人工相同的推进/拒绝路径；
- 单实例事务失败整笔回滚并记入 `failed`，不影响其他实例，下一轮重试；
- 后台守护线程周期触发（无外部 MQ/定时器）；进程重启后首轮扫描即恢复未完成升级。

## 鉴权边界

- `X-User` 是全部用户判断的唯一来源；待办查询强制 `assignee = 当前用户`，
  且 task_id 必须属于 URL 中的实例 ID——替换实例 ID 拿别人的 task_id 无法操作
  （返回 `task_instance_mismatch` / `task_not_pending`）。
- 撤回强制 `X-User == instance.submitter`。
