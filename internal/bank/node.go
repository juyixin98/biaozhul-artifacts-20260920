// Package bank 实现 Chandy–Lamport 原论文中的“转账守恒模型”节点。
//
// 教科书式银行语义，任意时刻（无丢包）恒有：
//
//		Σ 节点 balance + Σ 网络中未入账的在途转账金额 = 初始总额
//
//	  - 发起转账：立即从 balance 扣款，消息进入网络（钱在途中）；
//	  - 收款方：收到转账即入账；
//	  - 重复投递：按 TxnID 幂等去重，不重复入账。
//
// 快照守恒由 Chandy–Lamport 算法本身保证：节点记录的本地状态是当时余额；
// “记录时刻已在途中”的消息由快照管理器依据发送日志归入某一方（收款方在途
// 信道或发起方状态的补记），应用层无需在节点账务里做特殊处理。
package bank

import (
	"fmt"
)

// State 是快照记录的节点状态。
type State struct {
	Balance int `json:"balance"`
}

// Event 是节点视角的业务日志条目。
type Event struct {
	At      int64  `json:"at"`
	Node    int    `json:"node"`
	Kind    string `json:"kind"` // send / recv / drop_dup / insufficient
	Peer    int    `json:"peer"`
	Amount  int    `json:"amount,omitempty"`
	TxnID   int64  `json:"txn_id,omitempty"`
	Balance int    `json:"balance"`
	Note    string `json:"note,omitempty"`
}

// Sender 把一条转账消息送入网络。
type Sender func(to, amount int, txn int64)

// Node 是一个银行网点节点。
type Node struct {
	id      int
	balance int
	txnSeq  int64
	seenTxn map[[2]int64]bool
	send    Sender
	log     func(Event)
}

// New 创建节点。
func New(id, balance int, send Sender, log func(Event)) *Node {
	return &Node{
		id:      id,
		balance: balance,
		seenTxn: map[[2]int64]bool{},
		send:    send,
		log:     log,
	}
}

// ID 返回节点编号。
func (n *Node) ID() int { return n.id }

// Balance 返回当前余额。
func (n *Node) Balance() int { return n.balance }

// SnapshotState 返回快照用的节点状态。
func (n *Node) SnapshotState() any { return &State{Balance: n.balance} }

// Transfer 发起一笔向 to 的转账：立即扣款并发消息。余额不足返回 false。
func (n *Node) Transfer(to, amount int, at int64) bool {
	if amount <= 0 {
		panic(fmt.Sprintf("bank: 转账金额必须为正: %d", amount))
	}
	if amount > n.balance {
		n.log(Event{At: at, Node: n.id, Kind: "insufficient", Peer: to, Amount: amount, Balance: n.balance})
		return false
	}
	n.txnSeq++
	txn := n.txnSeq
	n.balance -= amount
	n.send(to, amount, txn)
	n.log(Event{At: at, Node: n.id, Kind: "send", Peer: to, Amount: amount, TxnID: txn, Balance: n.balance})
	return true
}

// Receive 处理一条投递到本节点的转账。dup 表示网络重复副本。
// 去重键为 (from, txn)：各节点的 TxnID 独立计数，必须连同发送方一起才唯一。
func (n *Node) Receive(from int, amount int, txn int64, at int64, dup bool) {
	key := dupKey(from, txn)
	if n.seenTxn[key] {
		n.log(Event{At: at, Node: n.id, Kind: "drop_dup", Peer: from, Amount: amount, TxnID: txn,
			Balance: n.balance, Note: "重复转账，已忽略"})
		return
	}
	n.seenTxn[key] = true
	n.balance += amount
	n.log(Event{At: at, Node: n.id, Kind: "recv", Peer: from, Amount: amount, TxnID: txn, Balance: n.balance})
}

// dupKey 是收款方去重的复合键。
func dupKey(from int, txn int64) [2]int64 { return [2]int64{int64(from), txn} }
