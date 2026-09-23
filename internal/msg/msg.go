// Package msg 定义模拟器各模块共享的基础消息类型。
package msg

// Kind 是网络帧内消息的类型。
type Kind int

const (
	// Data 是节点间的业务消息（单向转账）。
	Data Kind = iota
	// Marker 是 Chandy–Lamport 快照标记。
	Marker
)

// Message 是节点之间传递的逻辑消息。
type Message struct {
	Kind   Kind  `json:"kind"`              // 业务消息还是快照标记
	SnapID int   `json:"snap_id,omitempty"` // 仅 Marker 使用
	From   int   `json:"from"`              // 发送节点
	To     int   `json:"to"`                // 接收节点
	Amount int   `json:"amount,omitempty"`  // Data：转账金额
	TxnID  int64 `json:"txn_id,omitempty"`  // Data：事务号（幂等去重）
	MsgID  int   `json:"msg_id"`            // 发送方在该条逻辑信道上的单调编号
	SentAt int64 `json:"sent_at"`           // 发送时刻（逻辑时钟）
}

// IsMarker 报告该消息是否为快照标记。
func (m Message) IsMarker() bool { return m.Kind == Marker }
