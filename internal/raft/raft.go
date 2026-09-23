// Package raft 实现 Raft 论文（Ongaro & Ousterhout, 2014）的"安全子集"：
//   - 固定 3 节点，无成员变更、无快照、无日志压缩；
//   - 选举超时 + 心跳的领导者选举；
//   - 日志复制与冲突截断；
//   - 提交规则只提交当前任期的条目（§5.4.2），旧任期条目随当前任期条目一并提交；
//   - currentTerm / votedFor / log 在响应任何 RPC 或客户端写之前持久化（§5.3）。
//
// 节点不直接接触网络与时钟：由确定性离散事件模拟器注入 Tick 与消息，
// 节点产生的消息通过 Send 回调交给网络层。
package raft

import (
	"fmt"
	"math/rand"

	"raftdemo/internal/logstore"
)

// 角色常量。
const (
	Follower  = "follower"
	Candidate = "candidate"
	Leader    = "leader"
)

// 消息类型常量。
const (
	MsgVoteReq  = "voteReq"  // RequestVote RPC
	MsgVoteResp = "voteResp" // RequestVote 响应
	MsgAppReq   = "appReq"   // AppendEntries RPC（同时充当心跳）
	MsgAppResp  = "appResp"  // AppendEntries 响应
)

// Msg 是节点之间的全部 RPC 消息。用一个结构体承载四种 RPC，
// 未使用字段对零值，模拟器的网络层只搬运该结构体。
type Msg struct {
	Type string
	Term int
	From int
	To   int

	// RequestVote
	LastLogIndex int
	LastLogTerm  int

	// RequestVote 响应
	Granted bool

	// AppendEntries
	PrevLogIndex int
	PrevLogTerm  int
	Entries      []logstore.Entry
	LeaderCommit int

	// AppendEntries 响应（Success=false 时带回冲突提示，§5.3 优化）
	Success       bool
	ConflictIndex int
	ConflictTerm  int
}

// Config 是节点配置。选举窗口可按节点单独给出，模拟器据此构造
// 确定性的"预定选举结果"脚本。
type Config struct {
	ID             int
	N              int
	HeartbeatTicks int
	ElecLo         int // 选举超时下界（含）
	ElecHi         int // 选举超时上界（不含）
	Seed           int64
}

// Node 是一个 Raft 节点。持久化状态在 store 中；崩溃只清空易失状态，
// store 由外部决定是否保留。
type Node struct {
	cfg   Config
	store logstore.Store
	rng   *rand.Rand

	// 持久化状态（每次变更后立即 Save）。
	term     int
	votedFor int
	log      []logstore.Entry // 下标即日志索引，0 号为哨兵 {Term:0}

	// 易失状态。
	role             string
	leader           int
	commitIndex      int
	lastApplied      int
	electionDeadline int // 绝对 tick；follower/candidate 到点发起选举
	elecAttempts     int // 连续选举轮次，用于独立抖动
	nextIndex        map[int]int
	matchIndex       map[int]int
	grantedVotes     map[int]bool
	alive            bool
	now              int // 最近一次 Tick/Handle 的全局 tick，发送消息时用于计算投递时间

	// Send 由模拟器注入：把消息交给网络层。now 为发送时刻的全局 tick。
	Send func(now, from, to int, m Msg)
	// OnLeader 在节点成为某任期领导者时回调（模拟器用于校验"同一任期最多一个领导者"）。
	OnLeader func(id, term int)
	// OnApplied 在节点把一条日志应用到状态机时回调。
	OnApplied func(id int, e logstore.Entry)
}

// NewNode 从持久化存储启动一个节点（首次启动或重启都走这里）。
func NewNode(cfg Config, store logstore.Store) (*Node, error) {
	if cfg.N < 1 || cfg.ID < 0 || cfg.ID >= cfg.N {
		return nil, fmt.Errorf("非法节点配置: id=%d n=%d", cfg.ID, cfg.N)
	}
	if cfg.HeartbeatTicks <= 0 || cfg.ElecLo <= 0 || cfg.ElecHi <= cfg.ElecLo {
		return nil, fmt.Errorf("非法计时参数: heartbeat=%d window=[%d,%d)", cfg.HeartbeatTicks, cfg.ElecLo, cfg.ElecHi)
	}
	st, err := store.Load()
	if err != nil {
		return nil, fmt.Errorf("节点 %d 加载持久化状态失败: %w", cfg.ID, err)
	}
	if len(st.Log) == 0 {
		return nil, fmt.Errorf("节点 %d 日志缺少哨兵条目", cfg.ID)
	}
	n := &Node{
		cfg:        cfg,
		store:      store,
		rng:        rand.New(rand.NewSource(cfg.Seed)),
		term:       st.CurrentTerm,
		votedFor:   st.VotedFor,
		log:        st.Log,
		role:       Follower,
		leader:     -1,
		nextIndex:  map[int]int{},
		matchIndex: map[int]int{},
		alive:      true,
	}
	n.resetTimerAt(0, 0)
	return n, nil
}

// ---- 访问器（模拟器断言用）----

func (n *Node) ID() int               { return n.cfg.ID }
func (n *Node) Term() int             { return n.term }
func (n *Node) Role() string          { return n.role }
func (n *Node) Leader() int           { return n.leader }
func (n *Node) CommitIndex() int      { return n.commitIndex }
func (n *Node) LastApplied() int      { return n.lastApplied }
func (n *Node) LastLogIndex() int     { return len(n.log) - 1 }
func (n *Node) Alive() bool           { return n.alive }
func (n *Node) Now() int              { return n.now }
func (n *Node) Store() logstore.Store { return n.store }

// LogSnapshot 返回完整日志的深拷贝（包含 0 号哨兵）。
func (n *Node) LogSnapshot() []logstore.Entry {
	out := make([]logstore.Entry, len(n.log))
	copy(out, n.log)
	return out
}

// Committed 返回已提交日志的深拷贝（不含哨兵）。
func (n *Node) Committed() []logstore.Entry {
	out := make([]logstore.Entry, n.commitIndex)
	copy(out, n.log[1:n.commitIndex+1])
	return out
}

// ---- 持久化辅助 ----

func (n *Node) persist() error {
	return n.store.Save(logstore.State{
		CurrentTerm: n.term,
		VotedFor:    n.votedFor,
		Log:         n.log,
	})
}

// ---- 计时与选举 ----

// resetTimerAt 以当前 tick 为基准重置选举超时。
func (n *Node) resetTimerAt(now, attempts int) {
	n.elecAttempts = attempts
	span := n.cfg.ElecHi - n.cfg.ElecLo
	d := n.cfg.ElecLo
	if span > 0 {
		d += n.rng.Intn(span)
	}
	n.electionDeadline = now + d
}

// Tick 推进一个离散时间单位。
func (n *Node) Tick(now int) {
	n.now = now
	if !n.alive {
		return
	}
	switch n.role {
	case Leader:
		// 心跳固定周期广播。
		if now > 0 && now%n.cfg.HeartbeatTicks == 0 {
			n.broadcastAppendEntries()
		}
	case Follower, Candidate:
		if now >= n.electionDeadline {
			n.startElection(now)
		}
	}
}

func (n *Node) startElection(now int) {
	n.role = Candidate
	n.term++
	n.votedFor = n.cfg.ID
	n.leader = -1
	if err := n.persist(); err != nil { // 投票前必须先持久化任期与选票
		panic(err)
	}
	n.grantedVotes = map[int]bool{n.cfg.ID: true}
	lastIdx, lastTerm := n.LastLogIndex(), n.log[len(n.log)-1].Term
	for peer := 0; peer < n.cfg.N; peer++ {
		if peer == n.cfg.ID {
			continue
		}
		n.send(n.cfg.ID, peer, Msg{
			Type:         MsgVoteReq,
			Term:         n.term,
			From:         n.cfg.ID,
			To:           peer,
			LastLogIndex: lastIdx,
			LastLogTerm:  lastTerm,
		})
	}
	n.resetTimerAt(now, n.elecAttempts+1)
}

func (n *Node) becomeFollower(term int) {
	n.role = Follower
	n.leader = -1
	if term > n.term {
		n.term = term
		n.votedFor = -1
	}
}

func (n *Node) becomeLeader(now int) {
	n.role = Leader
	n.leader = n.cfg.ID
	lastIdx := n.LastLogIndex()
	n.nextIndex = map[int]int{}
	n.matchIndex = map[int]int{}
	for peer := 0; peer < n.cfg.N; peer++ {
		if peer == n.cfg.ID {
			continue
		}
		n.nextIndex[peer] = lastIdx + 1
		n.matchIndex[peer] = 0
	}
	// 当选后立即广播一次（论文：leader 立即发送空 AppendEntries）。
	n.appendNoop()
	n.broadcastAppendEntries()
	if n.OnLeader != nil {
		n.OnLeader(n.cfg.ID, n.term)
	}
}

// appendNoop 在赢得选举时追加一条当前任期的空操作日志并持久化。
// 该条目复制到多数派后即被提交（§5.4.2 只允许提交当前任期条目），
// 从而一并提交新领导者日志中来自之前任期的条目——这是论文 §8 给出的
// 标准做法，避免"旧任期条目已在多数派但无法推进 commitIndex"的窗口。
func (n *Node) appendNoop() {
	n.log = append(n.log, logstore.Entry{Term: n.term, Data: "", ClientID: ""})
	if err := n.persist(); err != nil {
		panic(err)
	}
}

// ---- 消息处理 ----

// Handle 处理一条入站 RPC 消息。now 为当前 tick，用于重置选举超时。
func (n *Node) Handle(now int, m Msg) {
	n.now = now
	if !n.alive {
		return
	}
	switch m.Type {
	case MsgVoteReq:
		n.handleVoteReq(now, m)
	case MsgVoteResp:
		n.handleVoteResp(now, m)
	case MsgAppReq:
		n.handleAppReq(now, m)
	case MsgAppResp:
		n.handleAppResp(now, m)
	}
}

func (n *Node) handleVoteReq(now int, m Msg) {
	if m.Term < n.term {
		n.send(n.cfg.ID, m.From, Msg{Type: MsgVoteResp, Term: n.term, From: n.cfg.ID, To: m.From, Granted: false})
		return
	}
	if m.Term > n.term {
		n.becomeFollower(m.Term)
	}
	grant := false
	switch {
	case n.role == Candidate && m.Term == n.term:
		// 同任期内我们已经投给自己，不能再投。
	case n.votedFor != -1 && n.votedFor != m.From:
		// 已投票给其他候选者。
	default:
		if n.candidateLogUpToDate(m.LastLogIndex, m.LastLogTerm) {
			grant = true
		}
	}
	if grant {
		n.votedFor = m.From
		if err := n.persist(); err != nil {
			panic(err)
		}
		// 收到合法的拉票请求，重置选举超时，避免与候选者争抢。
		n.resetTimerAt(now, 0)
	}
	n.send(n.cfg.ID, m.From, Msg{
		Type: MsgVoteResp, Term: n.term, From: n.cfg.ID, To: m.From, Granted: grant,
	})
}

// candidateLogUpToDate 实现 §5.2 选举限制：
// 候选者日志至少与本地日志一样新才给票（先比最后条目的任期，相同则比长度）。
func (n *Node) candidateLogUpToDate(candLastIndex, candLastTerm int) bool {
	myLastTerm := n.log[len(n.log)-1].Term
	myLastIndex := n.LastLogIndex()
	if candLastTerm != myLastTerm {
		return candLastTerm > myLastTerm
	}
	return candLastIndex >= myLastIndex
}

func (n *Node) handleVoteResp(now int, m Msg) {
	if m.Term > n.term {
		n.becomeFollower(m.Term)
		if err := n.persist(); err != nil {
			panic(err)
		}
		n.resetTimerAt(now, 0)
		return
	}
	if n.role != Candidate || m.Term != n.term || !m.Granted {
		return
	}
	n.grantedVotes[m.From] = true
	if len(n.grantedVotes) > n.cfg.N/2 {
		n.becomeLeader(now)
	}
}

func (n *Node) handleAppReq(now int, m Msg) {
	if m.Term < n.term {
		// 旧领导者：拒绝并带回当前任期，促使其下台。
		n.send(n.cfg.ID, m.From, Msg{
			Type: MsgAppResp, Term: n.term, From: n.cfg.ID, To: m.From,
			Success: false, ConflictIndex: n.LastLogIndex() + 1,
		})
		return
	}
	if m.Term > n.term {
		n.becomeFollower(m.Term)
	}
	if n.role == Candidate && m.Term == n.term {
		// 同任期已出现合格领导者（我们竞选失败），转为 follower。
		n.role = Follower
	}
	n.role = Follower
	n.leader = m.From
	n.resetTimerAt(now, 0)

	reply := Msg{Type: MsgAppResp, Term: n.term, From: n.cfg.ID, To: m.From}

	// 一致性检查：prevLogIndex/prevLogTerm 必须与本地匹配。
	if m.PrevLogIndex > n.LastLogIndex() {
		reply.Success = false
		reply.ConflictIndex = n.LastLogIndex() + 1
		reply.ConflictTerm = 0
		n.send(n.cfg.ID, m.From, reply)
		return
	}
	if n.log[m.PrevLogIndex].Term != m.PrevLogTerm {
		reply.Success = false
		reply.ConflictTerm = n.log[m.PrevLogIndex].Term
		// 带回该冲突任期第一条索引，leader 可一次跳过整个冲突任期。
		first := m.PrevLogIndex
		for first > 1 && n.log[first-1].Term == reply.ConflictTerm {
			first--
		}
		reply.ConflictIndex = first
		n.send(n.cfg.ID, m.From, reply)
		return
	}

	// 追加新条目；若与已有条目同索引不同任期，删除冲突段及其后的全部条目（§5.3）。
	truncateAt := -1
	for i, e := range m.Entries {
		idx := m.PrevLogIndex + 1 + i
		if idx <= n.LastLogIndex() {
			if n.log[idx].Term != e.Term {
				truncateAt = idx
				break
			}
		} else {
			break
		}
	}
	if truncateAt >= 0 {
		n.log = append([]logstore.Entry(nil), n.log[:truncateAt]...)
	}
	have := n.LastLogIndex() - m.PrevLogIndex
	if have < 0 {
		have = 0
	}
	if have < len(m.Entries) {
		// 深拷贝追加，避免与网络中转发的切片共享底层数组。
		fresh := make([]logstore.Entry, len(m.Entries)-have)
		copy(fresh, m.Entries[have:])
		n.log = append(n.log, fresh...)
	}
	if err := n.persist(); err != nil { // 响应前日志必须落盘
		panic(err)
	}

	// 推进提交点：follower 只能提交到领导者告知的位置。
	if m.LeaderCommit > n.commitIndex {
		target := m.LeaderCommit
		if target > n.LastLogIndex() {
			target = n.LastLogIndex()
		}
		n.commitIndex = target
		n.applyAll()
	}
	reply.Success = true
	reply.ConflictIndex = n.LastLogIndex()
	n.send(n.cfg.ID, m.From, reply)
}

func (n *Node) handleAppResp(now int, m Msg) {
	if m.Term > n.term {
		// 旧领导者发现自己过期：下台，持久化新任期。
		n.becomeFollower(m.Term)
		if err := n.persist(); err != nil {
			panic(err)
		}
		n.resetTimerAt(now, 0)
		return
	}
	if n.role != Leader || m.Term != n.term {
		return
	}
	if m.Success {
		// 成功时复用 ConflictIndex 字段携带 follower 追加后的最后日志索引。
		// 注意：follower 可能暂时保留了比 leader 更长的陈旧后缀（例如旧领导者
		// 隔离期间的未提交写入），空心跳不会截断它们。该进度不能超过 leader
		// 自己的日志长度，否则 nextIndex 会越过日志末尾导致越界；
		// 陈旧后缀会在 leader 后续发送同索引新任期条目时被截断覆盖。
		replicated := m.ConflictIndex
		if replicated > n.LastLogIndex() {
			replicated = n.LastLogIndex()
		}
		if replicated > n.matchIndex[m.From] {
			n.matchIndex[m.From] = replicated
		}
		if n.matchIndex[m.From]+1 > n.nextIndex[m.From] {
			n.nextIndex[m.From] = n.matchIndex[m.From] + 1
		}
		n.tryCommitAndApply()
		// 若还有未复制的条目，继续追赶。
		if n.nextIndex[m.From] <= n.LastLogIndex() {
			n.sendAppend(m.From)
		}
		return
	}
	// 失败：按 §5.3 的快速回退算法调整 nextIndex。
	// follower 在拒绝时权威地报告冲突位置：
	//   ConflictTerm>0：该任期在 follower 日志中的首索引；
	//   ConflictTerm=0：follower 日志比 PrevLogIndex 短，ConflictIndex 即其末索引+1。
	next := m.ConflictIndex
	if m.ConflictTerm > 0 {
		lastOfTerm := -1
		for i := n.LastLogIndex(); i >= 1; i-- {
			if n.log[i].Term == m.ConflictTerm {
				lastOfTerm = i
				break
			}
		}
		if lastOfTerm >= 0 {
			next = lastOfTerm + 1
		}
	}
	if next < 1 {
		next = 1
	}
	if next < n.nextIndex[m.From] {
		n.nextIndex[m.From] = next
		// follower 明确否认此前的匹配（例如它的日志被清空重置），
		// 陈旧的 matchIndex 必须同步下调，否则会错误计入提交多数派并阻碍回退。
		if n.matchIndex[m.From] > next-1 {
			n.matchIndex[m.From] = next - 1
		}
	}
	n.sendAppend(m.From)
}

// broadcastAppendEntries 向所有 peer 发送 AppendEntries（心跳或数据）。
func (n *Node) broadcastAppendEntries() {
	for peer := 0; peer < n.cfg.N; peer++ {
		if peer == n.cfg.ID {
			continue
		}
		n.sendAppend(peer)
	}
}

// sendAppend 按 nextIndex/matchIndex 组装单个 follower 的 AppendEntries。
func (n *Node) sendAppend(peer int) {
	ni := n.nextIndex[peer]
	if ni < 1 {
		// 防御性钳制：任何情况下 nextIndex 都不低于 1（索引 0 是哨兵）。
		ni = 1
		n.nextIndex[peer] = 1
	}
	prev := ni - 1
	var entries []logstore.Entry
	if ni <= n.LastLogIndex() {
		entries = make([]logstore.Entry, n.LastLogIndex()-ni+1)
		copy(entries, n.log[ni:])
	}
	n.send(n.cfg.ID, peer, Msg{
		Type:         MsgAppReq,
		Term:         n.term,
		From:         n.cfg.ID,
		To:           peer,
		PrevLogIndex: prev,
		PrevLogTerm:  n.log[prev].Term,
		Entries:      entries,
		LeaderCommit: n.commitIndex,
	})
}

// tryCommitAndApply 实现 §5.3/§5.4 的提交规则：
// 仅当多数派复制了某个 N，且 log[N].Term == 当前任期时才提交 N。
func (n *Node) tryCommitAndApply() {
	for cand := n.LastLogIndex(); cand > n.commitIndex; cand-- {
		if n.log[cand].Term != n.term {
			continue // §5.4.2：不能仅凭副本数提交之前任期的条目
		}
		count := 1 // 领导者自己
		for peer := 0; peer < n.cfg.N; peer++ {
			if peer == n.cfg.ID {
				continue
			}
			if n.matchIndex[peer] >= cand {
				count++
			}
		}
		if count > n.cfg.N/2 {
			n.commitIndex = cand
			n.applyAll()
			return
		}
	}
}

// applyAll 把已提交未应用的条目顺序应用到状态机。
func (n *Node) applyAll() {
	for n.lastApplied < n.commitIndex {
		n.lastApplied++
		e := n.log[n.lastApplied]
		if n.OnApplied != nil {
			n.OnApplied(n.cfg.ID, e)
		}
	}
}

// Submit 处理客户端写请求。只有当前任期的领导者接受：追加并持久化后
// 立即并发复制；确认（ack）在条目被提交并应用时由模拟器统一发出。
// 返回 false 表示非领导者，客户端必须改投其他节点。
func (n *Node) Submit(clientID, data string, now int) bool {
	n.now = now
	if !n.alive || n.role != Leader {
		return false
	}
	n.log = append(n.log, logstore.Entry{Term: n.term, Data: data, ClientID: clientID})
	if err := n.persist(); err != nil {
		panic(err)
	}
	n.broadcastAppendEntries()
	return true
}

func (n *Node) send(from, to int, m Msg) {
	if n.Send != nil {
		n.Send(n.now, from, to, m)
	}
}

// ---- 崩溃与重启 ----

// Crash 模拟节点崩溃：停止处理消息与时钟，易失状态丢弃。
// 持久化数据是否保留由外部对 store 的操作决定。
func (n *Node) Crash() {
	n.alive = false
}

// Restart 模拟进程重启：从 store 重新加载持久化状态，易失状态归零。
func (n *Node) Restart(now int) error {
	st, err := n.store.Load()
	if err != nil {
		return err
	}
	n.term = st.CurrentTerm
	n.votedFor = st.VotedFor
	n.log = st.Log
	n.role = Follower
	n.leader = -1
	n.commitIndex = 0
	n.lastApplied = 0
	n.nextIndex = map[int]int{}
	n.matchIndex = map[int]int{}
	n.grantedVotes = nil
	n.alive = true
	n.resetTimerAt(now, 0)
	return nil
}
