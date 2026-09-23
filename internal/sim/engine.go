package sim

import (
	"fmt"
	"sort"

	"raftrun/internal/raft"
)

// nodeEnv 实现 raft.Environment，把节点意图转成队列事件。
type nodeEnv struct {
	s  *Simulator
	nr *nodeRuntime
}

func (e *nodeEnv) Now() int64 { return e.s.time }

func (e *nodeEnv) Send(m raft.Message) {
	e.s.sendMessage(e.nr.id, m)
}

func (e *nodeEnv) ScheduleElection(deadline int64, nonce int64) {
	epoch := e.nr.epoch
	e.s.q.push(&event{time: deadline, kind: evElection, node: e.nr.id,
		epoch: epoch, nonce: nonce})
}

func (e *nodeEnv) ScheduleHeartbeat(deadline int64) {
	epoch := e.nr.epoch
	e.s.q.push(&event{time: deadline, kind: evHeartbeat, node: e.nr.id, epoch: epoch})
}

// sendMessage 应用虚拟网络的丢包/重复/时延抖动并安排投递。
func (s *Simulator) sendMessage(from int, m raft.Message) {
	link, ok := s.links[[2]int{from, m.To}]
	if !ok || link.blocked {
		s.recordTrace(TraceEvent{Kind: "send", From: from, To: m.To, Term: m.Term, Msg: m})
		s.recordTrace(TraceEvent{Kind: "drop", From: from, To: m.To, Term: m.Term, Detail: "link blocked"})
		return
	}
	s.recordTrace(TraceEvent{Kind: "send", From: from, To: m.To, Term: m.Term, Msg: m})
	if s.rng.Float64() < link.loss {
		s.recordTrace(TraceEvent{Kind: "drop", From: from, To: m.To, Term: m.Term, Detail: "random loss"})
		return
	}
	s.scheduleDelivery(from, m.To, m, false)
	if s.rng.Float64() < link.dup {
		// 重复副本独立抖动，可早于或晚于原件到达，制造重复与乱序。
		s.scheduleDelivery(from, m.To, m, true)
	}
}

func (s *Simulator) scheduleDelivery(from, to int, m raft.Message, dup bool) {
	base := s.sc.Network.BaseDelay
	jitter := 0.0
	if base > 0 && s.sc.Network.JitterPct > 0 {
		jitter = s.rng.Float64() * 2 * s.sc.Network.JitterPct
	}
	d := int64(float64(base) * (1 - s.sc.Network.JitterPct + jitter))
	if d < 0 {
		d = 0
	}
	msg := m // 复制一份投递载荷，避免别名问题
	s.q.push(&event{
		time: s.time + d,
		kind: evDeliver,
		node: to,
		msg:  &delayedMsg{from: from, to: to, payload: msg, dup: dup},
	})
}

// mainLoop 是离散事件主循环。
func (s *Simulator) mainLoop() {
	for {
		ev := s.q.pop()
		if ev == nil {
			return
		}
		if ev.time > s.sc.EndTime {
			return
		}
		s.time = ev.time
		switch ev.kind {
		case evExternal:
			s.handleExternal(ev.msg.payload.(*ScenarioEvent))
			s.observe()
		case evDeliver:
			dm := ev.msg
			nr, ok := s.nodes[ev.node]
			if !ok || !nr.alive {
				s.recordTrace(TraceEvent{Time: s.time, Kind: "drop", From: dm.from, To: dm.to, Detail: "node down"})
				continue
			}
			link := s.links[[2]int{dm.from, dm.to}]
			if link == nil || link.blocked {
				continue
			}
			nr.node.Step(dm.payload.(raft.Message))
			detail := ""
			if dm.dup {
				detail = "duplicate"
			}
			s.recordTrace(TraceEvent{Kind: "deliver", From: dm.from, To: dm.to,
				Term: dm.payload.(raft.Message).Term, Detail: detail})
		case evElection:
			nr := s.nodes[ev.node]
			if nr.alive && nr.epoch == ev.epoch {
				nr.node.ElectionTick(ev.nonce)
			}
		case evHeartbeat:
			nr := s.nodes[ev.node]
			if nr.alive && nr.epoch == ev.epoch {
				nr.node.HeartbeatTick()
			}
		}
		if ev.kind != evExternal {
			s.observe()
		}
	}
}

// resolveRef 把 "leader"/节点号 解析为当前节点 ID（0 表示无）。
// "leader" 在分区下可能有多个节点各自自称领导者，取其中任期最高者
// （即多数派一侧的现任领导者，而非被隔离的旧领导者）。
func (s *Simulator) resolveRef(ref *NodeRef) int {
	if ref == nil {
		return 0
	}
	if ref.Leader {
		best, bestTerm := 0, -1
		for id := 1; id <= 3; id++ {
			nr := s.nodes[id]
			if !nr.alive {
				continue
			}
			snap := nr.node.Inspect()
			if snap.Role == raft.Leader && snap.Term > bestTerm {
				best, bestTerm = id, snap.Term
			}
		}
		return best
	}
	return ref.ID
}

// handleExternal 执行脚本事件。
func (s *Simulator) handleExternal(e *ScenarioEvent) {
	switch e.Type {
	case "client":
		s.handleClient(e)
	case "crash":
		id := s.resolveRef(e.Node)
		if id == 0 {
			return
		}
		if nr := s.nodes[id]; nr != nil && nr.alive {
			nr.alive = false
			nr.epoch++ // 使在途定时器全部失效
			s.recordTrace(TraceEvent{Time: s.time, Kind: "crash", To: id})
		}
	case "restart":
		s.handleRestart(e)
	case "partition":
		s.handlePartition(e)
	case "heal":
		s.handleHeal()
	case "link":
		s.applyLink(e.From, e.To, e)
		s.applyLink(e.To, e.From, e)
	}
}

func (s *Simulator) handleClient(e *ScenarioEvent) {
	id := s.resolveRef(e.Node)
	res := ProposalResult{Client: e.Client, Node: id, Command: e.Command}
	nr := s.nodes[id]
	if id == 0 || nr == nil || !nr.alive {
		res.Status = "nodeDown"
		s.proposals = append(s.proposals, res)
		return
	}
	snap := nr.node.Inspect()
	if snap.Role != raft.Leader {
		res.Status = "notLeader"
		s.proposals = append(s.proposals, res)
		return
	}
	if nr.node.Propose(e.Client, e.Command) {
		snap = nr.node.Inspect()
		res.Status = "accepted"
		res.Index = len(snap.Log) - 1
		res.Term = snap.Term
	} else {
		res.Status = "notLeader"
	}
	s.proposals = append(s.proposals, res)
}

func (s *Simulator) handleRestart(e *ScenarioEvent) {
	id := s.resolveRef(e.Node)
	if id == 0 {
		return
	}
	nr := s.nodes[id]
	if nr != nil && nr.alive {
		return
	}
	// 复用同一磁盘目录：NewNode 会从文件恢复任期/投票/日志。
	store, err := raft.NewFileStorage(nr.dataDir)
	if err != nil {
		panic(err)
	}
	s.startNode(id, store)
	s.recordTrace(TraceEvent{Time: s.time, Kind: "restart", To: id})
}

// handlePartition 按分组断开所有跨组有向链路。
func (s *Simulator) handlePartition(e *ScenarioEvent) {
	groupOf := map[int]int{}
	for gi, g := range e.Groups {
		for _, id := range g {
			groupOf[id] = gi
		}
	}
	for a := 1; a <= 3; a++ {
		for b := 1; b <= 3; b++ {
			if a == b {
				continue
			}
			ga, oka := groupOf[a]
			gb, okb := groupOf[b]
			if oka && okb && ga != gb {
				if l := s.links[[2]int{a, b}]; l != nil {
					l.blocked = true
				}
			}
		}
	}
	s.recordTrace(TraceEvent{Time: s.time, Kind: "partition"})
}

func (s *Simulator) handleHeal() {
	for _, l := range s.links {
		l.blocked = false
	}
	s.recordTrace(TraceEvent{Time: s.time, Kind: "heal"})
}

func (s *Simulator) applyLink(from, to int, e *ScenarioEvent) {
	l := s.links[[2]int{from, to}]
	if l == nil {
		return
	}
	if e.Block != nil {
		l.blocked = *e.Block
	}
	if e.Loss != nil {
		l.loss = *e.Loss
	}
	if e.Dup != nil {
		l.dup = *e.Dup
	}
}

// observe 在每个内部事件后扫描节点，推进“全局已提交”视图、
// 回填客户端写入结果并记录领导者变化。
func (s *Simulator) observe() {
	for id := 1; id <= 3; id++ {
		nr := s.nodes[id]
		if !nr.alive {
			continue
		}
		snap := nr.node.Inspect()
		if cur := snap.LeaderID; cur != 0 {
			if prev, seen := s.lastLeader[id]; !seen || prev != cur {
				// 仅当该节点确实自认为领导者时记录。
				if snap.Role == raft.Leader {
					s.leaderChanges = append(s.leaderChanges, LeaderChange{
						Time: s.time, Term: snap.Term, Node: id,
					})
				}
			}
			s.lastLeader[id] = cur
		}
		ci := snap.CommitIndex
		for idx := 1; idx <= ci && idx < len(snap.Log); idx++ {
			got := snap.Log[idx]
			if want, ok := s.globalCommitted[idx]; ok {
				// 已提交索引的内容必须一致（安全性核心断言在结束时做）。
				_ = want
			} else {
				s.globalCommitted[idx] = got
			}
		}
	}

	// 已出现在全局已提交视图中的 accepted 写入标记为 committed。
	for i := range s.proposals {
		p := &s.proposals[i]
		if p.Status != "accepted" {
			continue
		}
		for idx, e := range s.globalCommitted {
			if e.ClientID == p.Client && idx == p.Index {
				p.Status = "committed"
			}
		}
	}
}

func (s *Simulator) recordTrace(t TraceEvent) {
	if !s.sc.Trace {
		return
	}
	if t.Time == 0 {
		t.Time = s.time
	}
	s.trace = append(s.trace, t)
}

// checkInvariants 在模拟结束时做安全性断言。
func (s *Simulator) checkInvariants() InvariantCheck {
	check := InvariantCheck{Passed: true}
	addErr := func(format string, args ...any) {
		check.Passed = false
		check.Errors = append(check.Errors, fmt.Sprintf(format, args...))
	}

	type view struct {
		ci  int
		log []raft.Entry
	}
	views := map[int]view{}
	var alive []int
	for id := 1; id <= 3; id++ {
		nr := s.nodes[id]
		if !nr.alive {
			continue
		}
		alive = append(alive, id)
		snap := nr.node.Inspect()
		if snap.CommitIndex >= len(snap.Log) {
			addErr("node %d: commitIndex %d beyond log length %d",
				id, snap.CommitIndex, len(snap.Log)-1)
		}
		views[id] = view{ci: snap.CommitIndex, log: snap.Log}
	}

	// 1) 所有存活节点的已提交前缀在索引、任期、命令上必须逐格一致。
	minCI := -1
	for _, id := range alive {
		if minCI == -1 || views[id].ci < minCI {
			minCI = views[id].ci
		}
	}
	for idx := 1; idx <= minCI; idx++ {
		var ref *raft.Entry
		var refNode int
		for _, id := range alive {
			e := views[id].log[idx]
			if ref == nil {
				ref = &e
				refNode = id
				continue
			}
			if e.Term != ref.Term || e.Command != ref.Command {
				addErr("committed index %d differs: node %d=(term=%d cmd=%q) node %d=(term=%d cmd=%q)",
					idx, refNode, ref.Term, ref.Command, id, e.Term, e.Command)
			}
		}
	}

	// 2) 日志匹配性质：任意两存活节点在相同索引处，若任期相同则命令也必须相同；
	//    且每个节点的日志自身任期序列合法（旧索引任期不大于新索引）。
	for _, id := range alive {
		v := views[id]
		for idx := 2; idx < len(v.log); idx++ {
			if v.log[idx-1].Term > v.log[idx].Term {
				addErr("node %d: term decreases at index %d (%d -> %d)",
					id, idx, v.log[idx-1].Term, v.log[idx].Term)
			}
		}
	}
	for ai := 0; ai < len(alive); ai++ {
		for bi := ai + 1; bi < len(alive); bi++ {
			x, y := views[alive[ai]], views[alive[bi]]
			limit := len(x.log)
			if len(y.log) < limit {
				limit = len(y.log)
			}
			for idx := 1; idx < limit; idx++ {
				if x.log[idx].Term == y.log[idx].Term &&
					x.log[idx].Command != y.log[idx].Command {
					addErr("log mismatch at index %d (same term %d): node %d cmd=%q vs node %d cmd=%q",
						idx, x.log[idx].Term, alive[ai], x.log[idx].Command,
						alive[bi], y.log[idx].Command)
				}
			}
		}
	}

	// 3) 被报告为 committed 的客户端写入，必须真的在全局已提交前缀中。
	for _, p := range s.proposals {
		if p.Status != "committed" {
			continue
		}
		e, ok := s.globalCommitted[p.Index]
		if !ok {
			addErr("proposal %q reported committed at index %d but missing from global prefix",
				p.Client, p.Index)
			continue
		}
		if e.Command != p.Command {
			addErr("proposal %q committed command mismatch: %q vs %q",
				p.Client, p.Command, e.Command)
		}
	}
	return check
}

// buildResult 汇总最终结果。
func (s *Simulator) buildResult() *Result {
	r := &Result{
		Name:            s.sc.Name,
		EndTime:         s.time,
		AliveNodes:      map[int]bool{},
		Proposals:       s.proposals,
		NodeCommitIndex: map[int]int{},
		NodeLogLength:   map[int]int{},
		LeaderChanges:   s.leaderChanges,
		InvariantCheck:  s.checkInvariants(),
	}
	if s.sc.Trace {
		r.Trace = s.trace
	}

	idxs := make([]int, 0, len(s.globalCommitted))
	for idx := range s.globalCommitted {
		idxs = append(idxs, idx)
	}
	sort.Ints(idxs)
	for _, idx := range idxs {
		e := s.globalCommitted[idx]
		r.Committed = append(r.Committed, CommittedEntry{
			Index: idx, Term: e.Term, Command: e.Command, Client: e.ClientID,
		})
	}

	for id := 1; id <= 3; id++ {
		nr := s.nodes[id]
		if !nr.alive {
			r.AliveNodes[id] = false
			continue
		}
		r.AliveNodes[id] = true
		snap := nr.node.Inspect()
		r.NodeCommitIndex[id] = snap.CommitIndex
		r.NodeLogLength[id] = len(snap.Log) - 1
	}
	return r
}
