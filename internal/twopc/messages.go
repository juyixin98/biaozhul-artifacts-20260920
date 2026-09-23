package twopc

// 消息类型（经典两阶段提交 + 终止协议）。
const (
	MsgPrepare         = "PREPARE"          // 协调者 -> 参与者：第一阶段
	MsgVoteCommit      = "VOTE_COMMIT"      // 参与者 -> 协调者：同意提交
	MsgVoteAbort       = "VOTE_ABORT"       // 参与者 -> 协调者：拒绝
	MsgGlobalCommit    = "GLOBAL_COMMIT"    // 协调者 -> 参与者：第二阶段提交
	MsgGlobalAbort     = "GLOBAL_ABORT"     // 协调者 -> 参与者：第二阶段中止
	MsgAck             = "ACK"              // 参与者 -> 协调者：第二阶段已持久化
	MsgDecisionRequest = "DECISION_REQUEST" // 参与者 -> 协调者/同伴：阻塞时询问决议
	MsgDecisionCommit  = "DECISION_COMMIT"  // 回复：决议为提交
	MsgDecisionAbort   = "DECISION_ABORT"   // 回复：决议为中止
)

// Message 是节点间通信的唯一载荷。
type Message struct {
	Type  string `json:"type"`
	TxnID string `json:"txnId"`
	From  string `json:"from"`
	To    string `json:"to"`
}
