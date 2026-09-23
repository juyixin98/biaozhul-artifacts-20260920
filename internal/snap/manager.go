// Package snap 实现 Chandy–Lamport 分布式快照算法。
//
// 前提（见论文条件）：
//   - 信道可靠且 FIFO：标记与业务消息走同一信道，标记不会越过业务消息；
//   - 节点收到某快照的第一个标记时：记录自身状态，并沿所有出信道发送该快照标记；
//   - 节点在“已记录状态”到“收到来自某邻居的标记”之间收到的业务消息，
//     全部记入该入信道的在途消息；
//
// 多个快照 ID 可以同时交叠进行，彼此状态完全隔离。
//
// 与“发起即扣款”银行模型的配套补记（out_after）：
// 节点记录本地状态时，此前发起的转账可能尚未到对端。FIFO 下，在对端记录状态
// 之后、入信道标记截止之前到达的钱会进入在途记录；但在入信道标记截止【之后】
// 才到达（或快照完成时仍未到达）的钱，既不在发送方状态（已扣款）也不在对端
// 状态/在途记录中。Build 时依据发送/投递日志把这部分金额补记为发送方状态的
// OutAfter，保证全局切面每笔钱唯一归属一次。
package snap

import (
	"sort"

	"simsnap/internal/msg"
)

// MarkerSender 负责把快照标记真正送入网络。
type MarkerSender func(from, to, snapID int)

// StateOf 返回节点当前待记录的状态。
type StateOf func(node int) any

// NodeState 是一个节点记录下来的本地状态。
type NodeState struct {
	Node     int   `json:"node"`
	State    any   `json:"state"`
	TakenAt  int64 `json:"taken_at"`
	OutAfter int   `json:"out_after,omitempty"` // Build 时补记：记录前发出、对端截止后才到达/未到达的金额
}

// ChannelState 是一条有向信道在快照中的记录。
type ChannelState struct {
	From   int           `json:"from"`
	To     int           `json:"to"`
	Msgs   []msg.Message `json:"msgs"`
	Closed bool          `json:"closed"`
}

// Snapshot 是一次全局快照（可能尚未完成）。
type Snapshot struct {
	ID           int            `json:"id"`
	Complete     bool           `json:"complete"`
	CompleteAt   int64          `json:"complete_at,omitempty"`
	Nodes        []NodeState    `json:"nodes"`
	Channels     []ChannelState `json:"channels"`
	MissingNodes []int          `json:"missing_nodes,omitempty"`
	OpenChannels [][2]int       `json:"open_channels,omitempty"`
}

type chanRec struct {
	msgs     []msg.Message
	closed   bool
	closedAt int64
}

type nodeRec struct {
	done    bool
	state   any
	takenAt int64
	in      map[int]*chanRec
}

type snapState struct {
	id         int
	nodes      map[int]*nodeRec
	complete   bool
	completeAt int64
}

type wireMsg struct {
	m       msg.Message
	arrived int64 // 0=截至 Build 尚未观测到投递
}

// Manager 管理若干个交叠进行的快照，所有方法仅在单线程事件循环中调用。
type Manager struct {
	n          int
	snaps      map[int]*snapState
	sendMarker MarkerSender
	stateOf    StateOf
	wire       []wireMsg
}

// NewManager 创建快照管理器。n 为节点数（全互联拓扑）。
func NewManager(n int, sendMarker MarkerSender, stateOf StateOf) *Manager {
	return &Manager{n: n, snaps: map[int]*snapState{}, sendMarker: sendMarker, stateOf: stateOf}
}

func (m *Manager) getOrCreate(id int) *snapState {
	s := m.snaps[id]
	if s == nil {
		s = &snapState{id: id, nodes: map[int]*nodeRec{}}
		m.snaps[id] = s
	}
	return s
}

func (m *Manager) nodeRec(s *snapState, node int) *nodeRec {
	r := s.nodes[node]
	if r == nil {
		r = &nodeRec{in: map[int]*chanRec{}}
		for j := 0; j < m.n; j++ {
			if j != node {
				r.in[j] = &chanRec{}
			}
		}
		s.nodes[node] = r
	}
	return r
}

// NoteSend 记录一条业务消息的发送（按 From+TxnID 去重）。
func (m *Manager) NoteSend(message msg.Message) {
	for _, w := range m.wire {
		if w.m.From == message.From && w.m.TxnID == message.TxnID {
			return
		}
	}
	m.wire = append(m.wire, wireMsg{m: message})
}

// SetSentAt 补记已登记消息的发送时刻（网络层在 Send 时统一打戳）。
func (m *Manager) SetSentAt(from int, txn int64, sentAt int64) {
	for i := range m.wire {
		w := &m.wire[i]
		if w.m.From == from && w.m.TxnID == txn {
			w.m.SentAt = sentAt
			return
		}
	}
}

// NoteDelivery 在业务消息实际投递时调用（重复副本只记首次到达时刻）。
func (m *Manager) NoteDelivery(message msg.Message, at int64) {
	for i := range m.wire {
		w := &m.wire[i]
		if w.m.From == message.From && w.m.TxnID == message.TxnID {
			if w.arrived == 0 {
				w.arrived = at
			}
			return
		}
	}
	m.wire = append(m.wire, wireMsg{m: message, arrived: at})
}

// Initiate 由发起节点启动一次快照（幂等）。
func (m *Manager) Initiate(id int, initiator int, at int64) bool {
	if _, ok := m.snaps[id]; ok {
		return false
	}
	s := m.getOrCreate(id)
	m.recordState(s, initiator, at)
	for j := 0; j < m.n; j++ {
		if j != initiator {
			m.sendMarker(initiator, j, id)
		}
	}
	return true
}

func (m *Manager) recordState(s *snapState, node int, at int64) {
	r := m.nodeRec(s, node)
	if r.done {
		return
	}
	r.done = true
	r.state = m.stateOf(node)
	r.takenAt = at
}

// MarkerReceived 处理 node 收到来自 from 的快照标记。
func (m *Manager) MarkerReceived(id, from, node int, at int64) {
	s := m.getOrCreate(id)
	r := m.nodeRec(s, node)
	if !r.done {
		m.recordState(s, node, at)
		for j := 0; j < m.n; j++ {
			if j != node {
				m.sendMarker(node, j, id)
			}
		}
	}
	if ch := r.in[from]; ch != nil && !ch.closed {
		ch.closed = true
		ch.closedAt = at
	}
	m.checkComplete(s, at)
}

// DataReceived 在 node 处理一条业务消息之前调用，返回该消息被哪些快照的在途记录捕获。
func (m *Manager) DataReceived(from, node int, message msg.Message) []int {
	var captured []int
	for _, id := range m.activeIDs(node) {
		s := m.snaps[id]
		ch := s.nodes[node].in[from]
		if ch != nil && !ch.closed {
			ch.msgs = append(ch.msgs, message)
			captured = append(captured, id)
		}
	}
	return captured
}

func (m *Manager) activeIDs(node int) []int {
	ids := make([]int, 0)
	for id, s := range m.snaps {
		if s.complete {
			continue
		}
		if r := s.nodes[node]; r != nil && r.done {
			ids = append(ids, id)
		}
	}
	sort.Ints(ids)
	return ids
}

func (m *Manager) checkComplete(s *snapState, at int64) {
	if s.complete || len(s.nodes) != m.n {
		return
	}
	for i := 0; i < m.n; i++ {
		r := s.nodes[i]
		if r == nil || !r.done {
			return
		}
		for j := 0; j < m.n; j++ {
			if j != i {
				if ch := r.in[j]; ch == nil || !ch.closed {
					return
				}
			}
		}
	}
	s.complete = true
	s.completeAt = at
}

// IsComplete 报告某快照是否已完成。
func (m *Manager) IsComplete(id int) bool {
	if s := m.snaps[id]; s != nil {
		return s.complete
	}
	return false
}

// CompletedIDs 返回所有已完成快照 ID（升序）。
func (m *Manager) CompletedIDs() []int {
	var ids []int
	for id, s := range m.snaps {
		if s.complete {
			ids = append(ids, id)
		}
	}
	sort.Ints(ids)
	return ids
}

// outAfter 计算某快照中某节点“记录前已扣款发出、但既未进对端余额也未进在途记录”
// 的金额合计。
func (m *Manager) outAfter(s *snapState, node int) int {
	sender := s.nodes[node]
	if sender == nil || !sender.done {
		return 0
	}
	total := 0
	for _, w := range m.wire {
		mm := w.m
		if mm.From != node || mm.SentAt >= sender.takenAt {
			continue // 切面之后才发出，与本快照无关
		}
		peer := s.nodes[mm.To]
		if peer == nil || !peer.done {
			continue
		}
		if w.arrived != 0 && w.arrived < peer.takenAt {
			continue // 对端记录之前到账：已进对端余额
		}
		ch := peer.in[node]
		if w.arrived != 0 && ch != nil && w.arrived <= ch.closedAt {
			continue // 在途窗口内到达：已在对端在途记录中
		}
		// 截止后到达，或快照完成时尚未到达：补记到发送方。
		total += mm.Amount
	}
	return total
}

// Build 生成某快照的可序列化报告（无论是否完成都可调用）。
func (m *Manager) Build(id int) Snapshot {
	s := m.snaps[id]
	out := Snapshot{ID: id}
	if s == nil {
		return out
	}
	out.Complete = s.complete
	out.CompleteAt = s.completeAt

	nodeIDs := make([]int, 0, len(s.nodes))
	for i := range s.nodes {
		nodeIDs = append(nodeIDs, i)
	}
	sort.Ints(nodeIDs)
	for _, i := range nodeIDs {
		r := s.nodes[i]
		if !r.done {
			out.MissingNodes = append(out.MissingNodes, i)
			continue
		}
		ns := NodeState{Node: i, State: r.state, TakenAt: r.takenAt}
		if s.complete {
			ns.OutAfter = m.outAfter(s, i)
		}
		out.Nodes = append(out.Nodes, ns)
	}
	sort.Ints(out.MissingNodes)

	for b := 0; b < m.n; b++ {
		r := s.nodes[b]
		if r == nil {
			continue
		}
		peers := make([]int, 0, len(r.in))
		for a := range r.in {
			peers = append(peers, a)
		}
		sort.Ints(peers)
		for _, a := range peers {
			ch := r.in[a]
			if len(ch.msgs) == 0 {
				if !ch.closed {
					out.OpenChannels = append(out.OpenChannels, [2]int{a, b})
				}
				continue
			}
			cs := ChannelState{From: a, To: b, Closed: ch.closed, Msgs: append([]msg.Message(nil), ch.msgs...)}
			if !ch.closed {
				out.OpenChannels = append(out.OpenChannels, [2]int{a, b})
			}
			out.Channels = append(out.Channels, cs)
		}
	}
	return out
}
