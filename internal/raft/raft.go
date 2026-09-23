// Package raft 实现固定 3 节点 Raft 的选举与日志复制核心逻辑。
//
// 本包不含任何真实网络、定时器协程或磁盘 IO：节点只通过 Environment
// 接口与外部（确定性离散事件模拟器）交互，因此节点逻辑可以在单进程内
// 被确定性地驱动。持久化的任期、投票与日志通过 Storage 接口落盘。
package raft

import (
	"math/rand"
)

// Role 为节点角色。
type Role int

const (
	Follower Role = iota
	Candidate
	Leader
)

func (r Role) String() string {
	switch r {
	case Candidate:
		return "candidate"
	case Leader:
		return "leader"
	default:
		return "follower"
	}
}

// Entry 是一条日志条目。ClientID 为模拟器中发起写入的标识，用于把
// 复制结果回传给对应客户端请求；索引为 1 起。
type Entry struct {
	Term     int    `json:"term"`
	Command  string `json:"command"`
	ClientID string `json:"clientId,omitempty"`
}

// MessageType 为节点间消息类型。
type MessageType int

const (
	MsgApp      MessageType = iota // 领导者的追加日志（兼心跳）
	MsgAppResp                     // 追加日志响应
	MsgVote                        // 候选人的拉票
	MsgVoteResp                    // 投票响应
)

// Message 是节点间的内部消息（不直接序列化为线路格式）。
type Message struct {
	Type         MessageType
	From         int
	To           int
	Term         int // 发送者当前任期
	PrevLogIdx   int
	PrevLogTerm  int
	Entries      []Entry // MsgApp 携带的新条目
	LeaderCommit int     // MsgApp 中领导者的 commitIndex
	Grant        bool    // 投票响应
	Success      bool    // 追加响应
	MatchIndex   int     // 追加成功时接收者的最新索引
}

// Config 是单个节点使用的时序参数（单位由模拟器定义，本项目为毫秒）。
type Config struct {
	ID              int
	Peers           []int
	HeartbeatPeriod int
	ElectionMin     int // 随机选举超时下界（含）
	ElectionMax     int // 随机选举超时上界（含）
}

// Environment 是节点与确定性模拟器之间的全部交互面。
type Environment interface {
	Now() int64
	Send(m Message)
	// ScheduleElection 在随机选举超时后触发一次选举检查；
	// nonce 由节点生成，节点每次重新排程都会换号，模拟器据此丢弃旧事件。
	ScheduleElection(deadline int64, nonce int64)
	// ScheduleHeartbeat 在给定时刻触发一次心跳广播（仅领导者）。
	ScheduleHeartbeat(deadline int64)
}

// Node 是一个 Raft 节点的全部内存状态。模拟器单线程驱动，
// 因此节点内部不加锁。
type Node struct {
	cfg   Config
	env   Environment
	rng   *rand.Rand
	store Storage

	// 持久化状态（每次修改后同步落盘）
	CurrentTerm int
	VotedFor    int     // 0 表示本任期尚未投票
	Log         []Entry // Log[0] 为哨兵，真实条目索引 = 切片下标

	// 易失状态
	role        Role
	commitIndex int
	leaderID    int

	// 领导者易失状态（按节点 ID 索引）
	nextIndex    map[int]int
	matchIndex   map[int]int
	grantedVotes map[int]bool // 候选人：本任期已收到的赞成票来源

	// electionNonce 在每次重新排程选举定时器时加一；旧定时器事件
	// 因 nonce 过期被模拟器丢弃，避免一次任期内堆积多个超时事件。
	electionNonce int64
}

// NewNode 创建一个节点：若磁盘上已有持久化状态则恢复（模拟重启），
// 否则以初始跟随者身份启动。两种情况都会重新排程选举定时器。
func NewNode(cfg Config, env Environment, store Storage, rng *rand.Rand) *Node {
	n := &Node{
		cfg:          cfg,
		env:          env,
		rng:          rng,
		store:        store,
		VotedFor:     0,
		Log:          []Entry{{Term: 0}},
		nextIndex:    make(map[int]int),
		matchIndex:   make(map[int]int),
		grantedVotes: make(map[int]bool),
	}
	if ps, err := store.Load(); err == nil && ps != nil {
		n.CurrentTerm = ps.CurrentTerm
		n.VotedFor = ps.VotedFor
		if len(ps.Log) > 0 {
			n.Log = ps.Log
		}
	}
	n.resetElectionTimer()
	return n
}

// ---- 持久化辅助 ----

func (n *Node) persist() {
	// 模拟器单线程驱动；持久化失败属于致命错误，直接 panic 暴露问题。
	if err := n.store.Save(PersistentState{
		CurrentTerm: n.CurrentTerm,
		VotedFor:    n.VotedFor,
		Log:         n.Log,
	}); err != nil {
		panic(err)
	}
}

// ---- 日志索引辅助 ----

func (n *Node) lastIndex() int {
	return len(n.Log) - 1
}

func (n *Node) termAt(idx int) int {
	if idx < 0 || idx >= len(n.Log) {
		return -1
	}
	return n.Log[idx].Term
}

// ---- 定时器 ----

func (n *Node) resetElectionTimer() {
	span := n.cfg.ElectionMax - n.cfg.ElectionMin + 1
	d := n.cfg.ElectionMin
	if span > 1 {
		d += n.rng.Intn(span)
	}
	n.electionNonce++
	n.env.ScheduleElection(n.env.Now()+int64(d), n.electionNonce)
}

func (n *Node) scheduleHeartbeat() {
	n.env.ScheduleHeartbeat(n.env.Now() + int64(n.cfg.HeartbeatPeriod))
}

// ---- 角色转换 ----

func (n *Node) becomeFollower(term int) {
	if term > n.CurrentTerm {
		n.CurrentTerm = term
		n.VotedFor = 0
		n.persist()
	}
	n.role = Follower
	n.leaderID = 0
	n.resetElectionTimer()
}

func (n *Node) becomeCandidate() {
	n.CurrentTerm++
	n.role = Candidate
	n.VotedFor = n.cfg.ID
	n.leaderID = 0
	n.grantedVotes = map[int]bool{n.cfg.ID: true}
	n.persist()

	for _, p := range n.cfg.Peers {
		if p == n.cfg.ID {
			continue
		}
		n.env.Send(Message{
			Type:        MsgVote,
			From:        n.cfg.ID,
			To:          p,
			Term:        n.CurrentTerm,
			PrevLogIdx:  n.lastIndex(),
			PrevLogTerm: n.termAt(n.lastIndex()),
		})
	}
	// 固定 3 节点：候选人自己一票不足以成领导者，必须再获得一张票。
	n.resetElectionTimer()
}

func (n *Node) becomeLeader() {
	n.role = Leader
	n.leaderID = n.cfg.ID
	last := n.lastIndex()
	for _, p := range n.cfg.Peers {
		if p == n.cfg.ID {
			continue
		}
		n.nextIndex[p] = last + 1
		n.matchIndex[p] = 0
	}
	// 立即发一轮心跳以宣告主权。
	n.broadcastAppend()
	n.scheduleHeartbeat()
}

// ---- 外部入口 ----

// Step 处理一条到达本节点的消息。
func (n *Node) Step(m Message) {
	switch m.Type {
	case MsgApp:
		n.onAppend(m)
	case MsgAppResp:
		n.onAppendResp(m)
	case MsgVote:
		n.onVote(m)
	case MsgVoteResp:
		n.onVoteResp(m)
	}
}

// ElectionTick 是选举超时触发事件。仅当事件携带的 nonce 为最新
// 排程代号时才有效，否则是被重置取代的陈旧事件。
func (n *Node) ElectionTick(nonce int64) {
	if nonce != n.electionNonce {
		return
	}
	if n.role != Leader {
		n.becomeCandidate()
	}
}

// HeartbeatTick 是心跳周期触发事件。
func (n *Node) HeartbeatTick() {
	if n.role == Leader {
		n.broadcastAppend()
		n.scheduleHeartbeat()
	}
}

// Propose 处理客户端写入请求，返回是否被领导者接受。
func (n *Node) Propose(clientID, command string) bool {
	if n.role != Leader {
		return false
	}
	n.Log = append(n.Log, Entry{
		Term:     n.CurrentTerm,
		Command:  command,
		ClientID: clientID,
	})
	n.persist()
	n.matchIndex[n.cfg.ID] = n.lastIndex()
	// 立刻并行追加，不必等待心跳。
	n.broadcastAppend()
	return true
}

// ---- 日志复制 ----

func (n *Node) broadcastAppend() {
	for _, p := range n.cfg.Peers {
		if p == n.cfg.ID {
			continue
		}
		n.sendAppend(p)
	}
}

func (n *Node) sendAppend(to int) {
	next := n.nextIndex[to]
	prevIdx := next - 1
	m := Message{
		Type:         MsgApp,
		From:         n.cfg.ID,
		To:           to,
		Term:         n.CurrentTerm,
		PrevLogIdx:   prevIdx,
		PrevLogTerm:  n.termAt(prevIdx),
		LeaderCommit: n.commitIndex,
	}
	if next <= n.lastIndex() {
		m.Entries = append([]Entry(nil), n.Log[next:]...)
	}
	n.env.Send(m)
}

func (n *Node) onAppend(m Message) {
	// 任期旧：拒绝。
	if m.Term < n.CurrentTerm {
		n.env.Send(Message{Type: MsgAppResp, From: n.cfg.ID, To: m.From,
			Term: n.CurrentTerm, Success: false, MatchIndex: n.lastIndex()})
		return
	}
	// 任期新或相等：回正为跟随者。
	if m.Term > n.CurrentTerm || n.role != Follower {
		n.becomeFollower(m.Term)
	}
	n.leaderID = m.From
	n.resetElectionTimer()

	// 一致性检查：prevLogIdx 处任期必须匹配。
	if m.PrevLogIdx > n.lastIndex() || n.termAt(m.PrevLogIdx) != m.PrevLogTerm {
		n.env.Send(Message{Type: MsgAppResp, From: n.cfg.ID, To: m.From,
			Term: n.CurrentTerm, Success: false, MatchIndex: n.lastIndex()})
		return
	}

	// 追加：在保留一致性前缀的前提下合并新条目。
	ti := m.PrevLogIdx + 1
	for i, e := range m.Entries {
		idx := ti + i
		if idx <= n.lastIndex() {
			if n.Log[idx].Term != e.Term {
				// 冲突：截断后续全部内容后重写。
				n.Log = append([]Entry(nil), n.Log[:idx]...)
				n.Log = append(n.Log, e)
			}
		} else {
			n.Log = append(n.Log, e)
		}
	}
	n.persist()

	match := m.PrevLogIdx + len(m.Entries)
	n.env.Send(Message{Type: MsgAppResp, From: n.cfg.ID, To: m.From,
		Term: n.CurrentTerm, Success: true, MatchIndex: match})

	// 推进已提交索引（领导者的 commitIndex 只能前移）。
	if m.LeaderCommit > n.commitIndex {
		if m.LeaderCommit < match {
			n.commitIndex = m.LeaderCommit
		} else {
			n.commitIndex = match
		}
	}
}

func (n *Node) onAppendResp(m Message) {
	if m.Term > n.CurrentTerm {
		n.becomeFollower(m.Term)
		return
	}
	if n.role != Leader || m.Term < n.CurrentTerm {
		// 非领导者或过期响应：忽略。
		return
	}
	if !m.Success {
		// 仅允许 nextIndex 后退，防止陈旧成功/失败响应互相覆盖。
		hint := m.MatchIndex + 1
		if hint < 1 {
			hint = 1
		}
		if hint < n.nextIndex[m.From] {
			n.nextIndex[m.From] = hint
			n.sendAppend(m.From)
		}
		return
	}
	if m.MatchIndex > n.matchIndex[m.From] {
		n.matchIndex[m.From] = m.MatchIndex
	}
	if m.MatchIndex+1 > n.nextIndex[m.From] {
		n.nextIndex[m.From] = m.MatchIndex + 1
	}
	n.advanceCommit()
	// 可能还有未发完的后续条目。
	if n.nextIndex[m.From] <= n.lastIndex() {
		n.sendAppend(m.From)
	}
}

// advanceCommit 只提交“当前任期”的条目（本项目的安全子集规则）：
// 在当前任期条目存在的前提下，取多数派复制到的最高索引。
func (n *Node) advanceCommit() {
	for idx := n.lastIndex(); idx > n.commitIndex; idx-- {
		if n.termAt(idx) != n.CurrentTerm {
			continue // 旧任期条目不直接计数提交
		}
		count := 1
		for _, p := range n.cfg.Peers {
			if p != n.cfg.ID && n.matchIndex[p] >= idx {
				count++
			}
		}
		if count*2 > len(n.cfg.Peers) {
			n.commitIndex = idx
			return
		}
	}
}

// ---- 选举 ----

// logUpToDate 实现 Raft 的候选人日志新旧比较：先比最后条目任期，
// 任期相同再比最后索引。
func (n *Node) logUpToDate(candLastIdx, candLastTerm int) bool {
	myLastTerm := n.termAt(n.lastIndex())
	if candLastTerm != myLastTerm {
		return candLastTerm > myLastTerm
	}
	return candLastIdx >= n.lastIndex()
}

func (n *Node) onVote(m Message) {
	grant := false

	if m.Term > n.CurrentTerm {
		n.becomeFollower(m.Term)
	}
	if m.Term >= n.CurrentTerm {
		if (n.VotedFor == 0 || n.VotedFor == m.From) && n.logUpToDate(m.PrevLogIdx, m.PrevLogTerm) {
			n.VotedFor = m.From
			n.persist()
			grant = true
		}
		// 收到合法拉票（无论是否投给它）都重置选举定时器，
		// 避免与一个活跃竞选者无谓分票。
		n.resetElectionTimer()
	}
	n.env.Send(Message{Type: MsgVoteResp, From: n.cfg.ID, To: m.From,
		Term: n.CurrentTerm, Grant: grant})
}

func (n *Node) onVoteResp(m Message) {
	if m.Term > n.CurrentTerm {
		n.becomeFollower(m.Term)
		return
	}
	if n.role != Candidate || m.Term < n.CurrentTerm || !m.Grant {
		return
	}
	n.grantedVotes[m.From] = true
	if len(n.grantedVotes)*2 > len(n.cfg.Peers) {
		n.becomeLeader()
	}
}

// ---- 读取接口（供模拟器观察状态）----

// Snapshot 是节点对外可见状态的值拷贝。
type Snapshot struct {
	ID          int
	Role        Role
	Term        int
	LeaderID    int
	CommitIndex int
	Log         []Entry
}

// Inspect 返回节点当前状态的副本，供模拟器做提交推进与断言。
func (n *Node) Inspect() Snapshot {
	return Snapshot{
		ID:          n.cfg.ID,
		Role:        n.role,
		Term:        n.CurrentTerm,
		LeaderID:    n.leaderID,
		CommitIndex: n.commitIndex,
		Log:         append([]Entry(nil), n.Log...),
	}
}
