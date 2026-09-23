package twopc

// 定时器种类。
const (
	TimerVoteTimeout = "voteTimeout" // 协调者：收齐投票的截止时间
	TimerResend      = "resend"      // 协调者：重发第二阶段决议 / 收集 ACK
	TimerQuery       = "query"       // 参与者：PREPARED 后周期性询问决议
)

// 崩溃注入点（钩子名称）。节点协议执行到这些位置时调用 Context.Hook，
// runner 依据场景配置决定是否在此刻让节点“崩溃”（立即终止当前处理）。
const (
	// 协调者
	HookCoordStartAppended   = "coordinator.start.appended"   // START 日志已 fsync，尚未发 PREPARE
	HookCoordBeforePrepares  = "coordinator.before.prepares"  // 即将发送 PREPARE
	HookCoordCommitAppended  = "coordinator.commit.appended"  // COMMIT 日志已 fsync（提交点之后），尚未发 GLOBAL_COMMIT
	HookCoordAbortAppended   = "coordinator.abort.appended"   // ABORT 日志已 fsync，尚未发 GLOBAL_ABORT
	HookCoordBeforeCommitMsg = "coordinator.before.commitMsg" // 即将发送 GLOBAL_COMMIT
	HookCoordBeforeAbortMsg  = "coordinator.before.abortMsg"  // 即将发送 GLOBAL_ABORT

	// 参与者
	HookPartBeforePrepared = "participant.before.prepared" // 尚未写 PREPARED
	HookPartAfterPrepared  = "participant.after.prepared"  // PREPARED 已 fsync，尚未发 VOTE_COMMIT
	HookPartBeforeVote     = "participant.before.vote"     // 即将发送投票
	HookPartBeforeCommit   = "participant.before.commit"   // 尚未写 COMMITTED
	HookPartAfterCommit    = "participant.after.commit"    // COMMITTED 已 fsync，尚未发 ACK
	HookPartBeforeAbort    = "participant.before.abort"    // 尚未写 ABORTED
	HookPartAfterAbort     = "participant.after.abort"     // ABORTED 已 fsync，尚未发 ACK
)

// Context 是节点可见的运行环境（由 runner 实现），屏蔽调度器与网络细节。
type Context interface {
	// Now 当前虚拟时刻。
	Now() int64
	// Send 发出一条消息（可能被网络丢弃/重复/乱序）。
	Send(m Message)
	// Timer 在 delay ticks 后向本节点投递一个定时器事件。
	Timer(kind, txnID string, delay int64)
	// Event 记录一条协议追踪事件（写入最终报告的 trace）。
	Event(kind, txnID string, detail map[string]any)
	// Hook 协议崩溃注入点。返回 true 表示节点已在此刻“崩溃”，
	// 节点必须立即中止当前处理（后续副作用不得发生）。
	Hook(point, txnID string) bool
}

// 节点易失事务视图。
const (
	StateUnknown    = "unknown"   // 无任何记录
	StatePreparing  = "preparing" // 协调者：已 START，决议未定
	StateCommitting = "committing"
	StateAborting   = "aborting"
	StatePrepared   = "prepared"  // 参与者：已投赞成票，持锁等待决议
	StateCommitted  = "committed" // 已提交
	StateAborted    = "aborted"   // 已中止
)

// TxnView 是快照中单个事务在某节点上的状态。
type TxnView struct {
	State     string `json:"state"`
	LockHeld  bool   `json:"lockHeld"`
	SinceTick int64  `json:"sinceTick"`
}

// NodeSnapshot 是节点（或其磁盘）在报告时刻的状态视图。
type NodeSnapshot struct {
	ID        string             `json:"id"`
	Role      string             `json:"role"` // coordinator | participant
	TxnStates map[string]TxnView `json:"txnStates"`
}

// Node 是协调者/参与者的共同接口，由 runner 驱动。
type Node interface {
	ID() string
	// Begin 在协调者上开启一个新事务（仅协调者实现有实际效果）。
	Begin(txnID string)
	OnMessage(m Message)
	OnTimer(kind, txnID string)
	// Recover 在崩溃重启后调用：节点重放 WAL，恢复状态并重启必要的定时器/重发。
	Recover()
	// Snapshot 输出当前易失+持久状态（仅存活节点可调用）。
	Snapshot() NodeSnapshot
	Close() error
}
