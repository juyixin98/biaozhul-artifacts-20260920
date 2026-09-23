package sim

import (
	"fmt"
	mathrand "math/rand"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"raftdemo/internal/logstore"
	"raftdemo/internal/network"
	"raftdemo/internal/raft"
)

// pendingWrite 跟踪一个尚未确认的客户端写。
type pendingWrite struct {
	data string
	node int // 提交给的节点（-1 表示当时自动选择领导者失败）
	term int
}

// Engine 执行一份脚本。
type Engine struct {
	script *Script

	rng     *mathrand.Rand
	nodes   []*raft.Node
	stores  []logstore.Store
	network *network.Network

	// atTick[at] 是该 tick 按脚本顺序执行的事件列表。
	atTick map[int][]Event
	// writes 按 client 跟踪客户端写。
	writes map[string]*pendingWrite
	// acked 记录已经确认的 client（顺序保留在 acks 中）。
	acks []AckView
	// termLeaders 记录每个任期出现过的领导者（安全断言：每个任期至多一个）。
	termLeaders map[int]int

	failures []AssertionFailure
	trace    []TraceEntry
	maxTrace int
}

// Run 执行脚本并返回结果。失败的断言/不变量记录在 Result.Failures 中，
// 此时 Result.OK 为 false。
func Run(s *Script) Result {
	return RunWithTrace(s, 0)
}

// RunWithTrace 与 Run 相同，但 maxTrace>0 时收集最多 maxTrace 条运行轨迹。
func RunWithTrace(s *Script, maxTrace int) Result {
	e := &Engine{
		script:      s,
		atTick:      map[int][]Event{},
		writes:      map[string]*pendingWrite{},
		termLeaders: map[int]int{},
		maxTrace:    maxTrace,
	}
	return e.run()
}

func (e *Engine) fail(at int, kind, format string, args ...any) {
	e.failures = append(e.failures, AssertionFailure{
		At: at, Kind: kind, Message: fmt.Sprintf(format, args...),
	})
}

func (e *Engine) log(t TraceEntry) {
	if e.maxTrace > 0 && len(e.trace) < e.maxTrace {
		e.trace = append(e.trace, t)
	}
}

func (e *Engine) run() Result {
	s := e.script
	e.rng = mathrand.New(mathrand.NewSource(s.Seed))
	e.network = network.New(s.Nodes, mathrand.New(mathrand.NewSource(s.Seed+1)))

	// 全局网络默认参数。
	for i := 0; i < s.Nodes; i++ {
		for j := 0; j < s.Nodes; j++ {
			if i == j {
				continue
			}
			e.network.SetLink(i, j, &network.Link{
				DelayTicks: s.Network.DelayTicks,
				Jitter:     s.Network.Jitter,
				LossRate:   s.Network.LossRate,
				DupRate:    s.Network.DupRate,
			})
		}
	}

	// 事件索引化。
	for _, ev := range s.Events {
		e.atTick[ev.At] = append(e.atTick[ev.At], ev)
	}

	// 构建节点与存储。
	e.nodes = make([]*raft.Node, s.Nodes)
	e.stores = make([]logstore.Store, s.Nodes)
	for id := 0; id < s.Nodes; id++ {
		e.stores[id] = e.makeStore(id)
		lo, hi := s.ElectionLo, s.ElectionHi
		for _, w := range s.NodeWindows {
			if w.Node == id {
				lo, hi = w.Lo, w.Hi
			}
		}
		node, err := raft.NewNode(raft.Config{
			ID: id, N: s.Nodes,
			HeartbeatTicks: s.Heartbeat,
			ElecLo:         lo, ElecHi: hi,
			Seed: s.Seed + int64(id)*7919 + 1,
		}, e.stores[id])
		if err != nil {
			return Result{OK: false, Failures: []AssertionFailure{{
				At: 0, Kind: "config", Message: err.Error(),
			}}}
		}
		node.Send = func(now, from, to int, m raft.Msg) {
			// 发送时源节点已崩溃则消息作废（崩溃瞬间尚未进入网络的报文不再发出）。
			if !e.nodes[from].Alive() {
				return
			}
			e.network.Send(now, m)
		}
		node.OnLeader = func(nid, term int) {
			if prev, ok := e.termLeaders[term]; ok && prev != nid {
				e.fail(0, "election_safety",
					"任期 %d 出现两个领导者：节点 %d 与节点 %d", term, prev, nid)
			} else if !ok {
				e.termLeaders[term] = nid
			}
			e.log(TraceEntry{At: e.nodes[nid].Now(), Event: "leader", Node: nid, Term: term})
		}
		node.OnApplied = func(nid int, en logstore.Entry) {
			e.log(TraceEntry{At: e.nodes[nid].Now(), Event: "apply", Node: nid, Term: en.Term, Data: en.Data, Client: en.ClientID})
			if en.ClientID == "" {
				return
			}
			// 只有提交时仍是该客户端所请求的那个领导者，才确认写请求。
			if w, ok := e.writes[en.ClientID]; ok {
				if e.nodes[nid].Role() == raft.Leader && w.node == nid {
					idx := -1
					snap := e.nodes[nid].LogSnapshot()
					for i := 1; i < len(snap); i++ {
						if snap[i].ClientID == en.ClientID {
							idx = i
							break
						}
					}
					e.acks = append(e.acks, AckView{
						Client: en.ClientID, Data: en.Data, Leader: nid,
						Term: w.term, AppliedAt: -1, Index: idx,
					})
					// 立即移除：其他 follower 随后也会应用同一条目，不能重复确认。
					delete(e.writes, en.ClientID)
				}
			}
		}
		e.nodes[id] = node
	}

	// 主循环。
	for now := 0; now <= s.Ticks; now++ {
		// 1) 脚本事件（写、崩溃、分区、断言）。
		for _, ev := range e.atTick[now] {
			e.dispatch(now, ev)
		}
		// 2) 投递本 tick 到期的网络消息（崩溃节点不接收）。
		for _, m := range e.network.Tick(now) {
			if e.nodes[m.To].Alive() {
				e.nodes[m.To].Handle(now, m)
			}
		}
		// 3) 节点时钟。
		for _, n := range e.nodes {
			n.Tick(now)
		}
		// 4) 回填确认时刻并做周期性安全检查。
		e.stampAcks(now)
		e.checkCommitAgreement(now)
	}

	// 收尾：安全不变量终检。
	e.finalAckSafety()
	res := e.buildResult()
	res.TicksRun = s.Ticks
	res.Network = NetworkStats{
		Dropped: e.network.Dropped(), Duplicated: e.network.Duplicated(),
	}
	res.OK = len(e.failures) == 0
	return res
}

func (e *Engine) makeStore(id int) logstore.Store {
	switch e.script.Storage {
	case "file":
		if e.script.StateDir == "" {
			// 未指定目录时退回内存存储并记录（CLI 层会先校验，这里只是防御）。
			return logstore.NewMemStore()
		}
		if id == 0 && e.script.FreshState {
			// 仅在创建第一个节点存储前清空一次，保证脚本可重复运行。
			if err := os.RemoveAll(e.script.StateDir); err != nil {
				panic(err)
			}
		}
		dir := filepath.Join(e.script.StateDir, fmt.Sprintf("node%d", id))
		st, err := logstore.NewFileStore(dir)
		if err != nil {
			panic(err)
		}
		return st
	default:
		return logstore.NewMemStore()
	}
}

func (e *Engine) dispatch(now int, ev Event) {
	switch ev.Kind {
	case "client_write":
		e.doClientWrite(now, ev)
	case "crash":
		id := e.resolveTarget(ev)
		if id >= 0 {
			e.nodes[id].Crash()
			e.log(TraceEntry{At: now, Event: "crash", Node: id})
		}
	case "restart":
		id := e.resolveTarget(ev)
		if id >= 0 {
			if err := e.nodes[id].Restart(now); err != nil {
				e.fail(now, "restart", "节点 %d 重启失败: %v", id, err)
			}
			e.log(TraceEntry{At: now, Event: "restart", Node: id, Term: e.nodes[id].Term()})
		}
	case "wipe":
		id := e.resolveTarget(ev)
		if id >= 0 {
			if err := e.stores[id].Wipe(); err != nil {
				e.fail(now, "wipe", "节点 %d 清盘失败: %v", id, err)
			}
			if e.nodes[id].Alive() {
				// 清盘仅在停机状态下有意义；若仍在运行，强制崩溃。
				e.nodes[id].Crash()
			}
			e.log(TraceEntry{At: now, Event: "wipe", Node: id})
		}
	case "partition":
		if len(ev.Partition) != e.script.Nodes {
			e.fail(now, "partition", "partition 长度必须等于节点数")
			return
		}
		e.network.SetPartition(ev.Partition, ev.Heal)
		e.log(TraceEntry{At: now, Event: "partition", Detail: fmt.Sprint(ev.Partition)})
	case "isolate":
		if ev.Isolated == nil {
			e.fail(now, "isolate", "缺少 isolated 字段")
			return
		}
		e.network.Isolate(*ev.Isolated, ev.Heal)
		e.log(TraceEntry{At: now, Event: "isolate", Node: *ev.Isolated})
	case "link":
		if ev.From == nil || ev.To == nil {
			e.fail(now, "link", "缺少 from/to 字段")
			return
		}
		e.network.SetLink(*ev.From, *ev.To, ev.Link)
	case "network_reset":
		e.network.ResetAll()
		e.log(TraceEntry{At: now, Event: "network_reset"})
	case "committed":
		e.assertCommitted(now, ev)
	case "not_acked":
		e.assertNotAcked(now, ev)
	case "log_contains":
		e.assertLogContains(now, ev)
	case "log_match":
		e.assertLogMatch(now, ev)
	case "commit_index":
		e.assertCommitIndex(now, ev)
	}
}

func (e *Engine) resolveTarget(ev Event) int {
	if ev.Target != nil {
		return *ev.Target
	}
	return -1
}

// currentLeader 返回当前存活且自称为 leader 的节点，找不到返回 -1。
// 网络分区时可能短暂存在两个自称 leader（旧的尚未见到更高任期），
// 此时选择任期最高的那个——它才是多数派认可的领导者。
func (e *Engine) currentLeader() int {
	best, bestTerm := -1, -1
	for _, n := range e.nodes {
		if n.Alive() && n.Role() == raft.Leader && n.Term() > bestTerm {
			best, bestTerm = n.ID(), n.Term()
		}
	}
	return best
}

func (e *Engine) doClientWrite(now int, ev Event) {
	if ev.Client == "" {
		e.fail(now, "client_write", "缺少 client 字段")
		return
	}
	if _, dup := e.writes[ev.Client]; dup {
		e.fail(now, "client_write", "client %q 有未完成的重复写请求", ev.Client)
		return
	}
	target := -1
	if ev.Node != nil {
		target = *ev.Node
	} else {
		target = e.currentLeader()
	}
	if target < 0 {
		// 无领导者：写请求立即失败，不产生任何日志（客户端稍后可重试）。
		e.writes[ev.Client] = &pendingWrite{data: ev.Data, node: -1, term: -1}
		e.log(TraceEntry{At: now, Event: "write_rejected", Client: ev.Client, Data: ev.Data, Detail: "no leader"})
		return
	}
	ok := e.nodes[target].Submit(ev.Client, ev.Data, now)
	if !ok {
		e.writes[ev.Client] = &pendingWrite{data: ev.Data, node: -1, term: -1}
		e.log(TraceEntry{At: now, Event: "write_rejected", Node: target, Client: ev.Client, Data: ev.Data, Detail: "not leader"})
		return
	}
	e.writes[ev.Client] = &pendingWrite{
		data: ev.Data, node: target, term: e.nodes[target].Term(),
	}
	e.log(TraceEntry{At: now, Event: "write", Node: target, Client: ev.Client, Data: ev.Data, Term: e.nodes[target].Term()})
}

// ---- 断言 ----

func (e *Engine) isAcked(client string) bool {
	for _, a := range e.acks {
		if a.Client == client {
			return true
		}
	}
	return false
}

// assertCommitted：默认要求所有存活节点的已提交前缀都包含该 client 的数据；
// quorum=true 时要求多数派存活节点共同提交到包含该条目的位置。
func (e *Engine) assertCommitted(now int, ev Event) {
	if ev.Client == "" || ev.ExpectIndex == nil {
		e.fail(now, "committed", "committed 断言需要 client 与 expect_index")
		return
	}
	if !e.isAcked(ev.Client) {
		e.fail(now, "committed", "写请求 %q 尚未得到确认（acks=%v）", ev.Client, e.ackClients())
		return
	}
	check := func(n *raft.Node) bool {
		if n.CommitIndex() < *ev.ExpectIndex {
			return false
		}
		return dataAt(n.Committed(), *ev.ExpectIndex) == ev.Data
	}
	if ev.Quorum != nil && *ev.Quorum {
		count := 0
		for _, n := range e.nodes {
			if n.Alive() && check(n) {
				count++
			}
		}
		if count <= e.script.Nodes/2 {
			e.fail(now, "committed", "client %q 的数据仅在 %d 个存活节点提交到索引 %d，不足多数派",
				ev.Client, count, *ev.ExpectIndex)
		}
		return
	}
	for _, n := range e.nodes {
		if n.Alive() && !check(n) {
			e.fail(now, "committed", "节点 %d 的已提交索引 %d 不含 %q@%d",
				n.ID(), n.CommitIndex(), ev.Data, *ev.ExpectIndex)
		}
	}
}

// assertNotAcked：多数派失联时不确认写入。
func (e *Engine) assertNotAcked(now int, ev Event) {
	if ev.Client == "" {
		e.fail(now, "not_acked", "需要 client 字段")
		return
	}
	if e.isAcked(ev.Client) {
		e.fail(now, "not_acked", "写请求 %q 不应被确认，但已确认: %+v", ev.Client, e.acks)
	}
}

// assertLogContains：检查某节点日志中按序包含给定数据片段。
// full_log=true 检查完整日志（含未提交），否则只检查已提交前缀。
func (e *Engine) assertLogContains(now int, ev Event) {
	id := e.resolveTarget(ev)
	if id < 0 {
		e.fail(now, "log_contains", "缺少 target")
		return
	}
	var entries []logstore.Entry
	if ev.FullLog != nil && *ev.FullLog {
		entries = e.nodes[id].LogSnapshot()
	} else {
		entries = e.nodes[id].Committed()
	}
	var got []string
	for _, en := range entries {
		got = append(got, en.Data)
	}
	if !containsSubsequence(got, strings.Fields(ev.Data)) {
		e.fail(now, "log_contains", "节点 %d 日志 %v 不包含子序列 %v", id, got, strings.Fields(ev.Data))
	}
}

// assertLogMatch：所有存活节点的完整日志（含未提交后缀）逐索引完全一致。
// 用于网络恢复、冲突覆盖完成后断言日志收敛。
func (e *Engine) assertLogMatch(now int, ev Event) {
	var ref *raft.Node
	for _, n := range e.nodes {
		if n.Alive() {
			ref = n
			break
		}
	}
	if ref == nil {
		e.fail(now, "log_match", "没有存活节点可比较")
		return
	}
	refLog := ref.LogSnapshot()
	for _, n := range e.nodes {
		if !n.Alive() || n.ID() == ref.ID() {
			continue
		}
		other := n.LogSnapshot()
		if len(other) != len(refLog) {
			e.fail(now, "log_match",
				"节点 %d 日志长度 %d 与节点 %d 的 %d 不一致",
				n.ID(), len(other)-1, ref.ID(), len(refLog)-1)
			return
		}
		for k := 1; k < len(refLog); k++ {
			if other[k].Term != refLog[k].Term || other[k].Data != refLog[k].Data {
				e.fail(now, "log_match",
					"节点 %d 与节点 %d 在索引 %d 处不一致：(%d,%q) vs (%d,%q)",
					n.ID(), ref.ID(), k, other[k].Term, other[k].Data, refLog[k].Term, refLog[k].Data)
				return
			}
		}
	}
}

// assertCommitIndex：检查节点（或全部存活节点）的 commitIndex。
func (e *Engine) assertCommitIndex(now int, ev Event) {
	if ev.ExpectIndex == nil {
		e.fail(now, "commit_index", "缺少 expect_index")
		return
	}
	if ev.Target != nil {
		if ci := e.nodes[*ev.Target].CommitIndex(); ci != *ev.ExpectIndex {
			e.fail(now, "commit_index", "节点 %d commitIndex=%d，期望 %d", *ev.Target, ci, *ev.ExpectIndex)
		}
		return
	}
	for _, n := range e.nodes {
		if n.Alive() && n.CommitIndex() != *ev.ExpectIndex {
			e.fail(now, "commit_index", "节点 %d commitIndex=%d，期望 %d", n.ID(), n.CommitIndex(), *ev.ExpectIndex)
		}
	}
}

// ---- 周期性安全检查 ----

// checkCommitAgreement 是验收核心：任意时刻，任意两个存活节点的已提交日志
// 必须在共同索引上完全一致（领导者完整性 + 日志匹配性质的可观察推论）。
func (e *Engine) checkCommitAgreement(now int) {
	for i := 0; i < len(e.nodes); i++ {
		for j := i + 1; j < len(e.nodes); j++ {
			a, b := e.nodes[i], e.nodes[j]
			if !a.Alive() || !b.Alive() {
				continue
			}
			ca, cb := a.Committed(), b.Committed()
			common := len(ca)
			if len(cb) < common {
				common = len(cb)
			}
			for k := 0; k < common; k++ {
				if ca[k].Term != cb[k].Term || ca[k].Data != cb[k].Data {
					e.fail(now, "commit_agreement",
						"节点 %d 与节点 %d 在已提交索引 %d 处不一致：(%d,%q) vs (%d,%q)",
						a.ID(), b.ID(), k+1, ca[k].Term, ca[k].Data, cb[k].Term, cb[k].Data)
					return
				}
			}
		}
	}
}

// finalAckSafety：所有确认过的写入，其条目必须仍存在于多数派节点的日志中，
// 且提交内容一致（防止错误确认后条目被截断覆盖）。
func (e *Engine) finalAckSafety() {
	for _, a := range e.acks {
		count := 0
		for _, n := range e.nodes {
			log := n.LogSnapshot()
			if a.Index >= 1 && a.Index < len(log) {
				if en := log[a.Index]; en.Data == a.Data && en.Term == a.Term {
					count++
				}
			}
		}
		if count <= e.script.Nodes/2 {
			e.fail(e.script.Ticks, "ack_safety",
				"已确认的写入 %q（client=%s, 索引=%d）仅存在于 %d 个节点日志中，不足多数派",
				a.Data, a.Client, a.Index, count)
		}
	}
}

// stampAcks 在每 tick 末把确认时刻补上（OnApplied 回调内不知道全局 tick）。
func (e *Engine) stampAcks(now int) {
	for i := range e.acks {
		if e.acks[i].AppliedAt == -1 {
			e.acks[i].AppliedAt = now
		}
	}
}

func (e *Engine) ackClients() []string {
	out := make([]string, 0, len(e.acks))
	for _, a := range e.acks {
		out = append(out, a.Client)
	}
	return out
}

// ---- 结果组装 ----

func (e *Engine) buildResult() Result {
	nr := make([]NodeResult, len(e.nodes))
	for i, n := range e.nodes {
		committed := n.Committed()
		if committed == nil {
			committed = []logstore.Entry{}
		}
		full := n.LogSnapshot()[1:]
		if full == nil {
			full = []logstore.Entry{}
		}
		nr[i] = NodeResult{
			ID: n.ID(), Alive: n.Alive(), Role: n.Role(), Term: n.Term(),
			CommitIndex: n.CommitIndex(),
			Committed:   toViews(committed),
			FullLog:     toViews(full),
		}
	}
	hist := make([]LeaderView, 0, len(e.termLeaders))
	for term, leader := range e.termLeaders {
		hist = append(hist, LeaderView{Term: term, Leader: leader})
	}
	sort.Slice(hist, func(i, j int) bool { return hist[i].Term < hist[j].Term })
	acks := e.acks
	if acks == nil {
		acks = []AckView{}
	}
	failures := e.failures
	if failures == nil {
		failures = []AssertionFailure{}
	}
	return Result{
		TermHistory: hist,
		Acks:        acks,
		Nodes:       nr,
		Failures:    failures,
		Trace:       e.trace,
	}
}

func toViews(entries []logstore.Entry) []EntryView {
	out := make([]EntryView, 0, len(entries))
	for i, en := range entries {
		out = append(out, EntryView{
			Index: i + 1, Term: en.Term, Data: en.Data, ClientID: en.ClientID,
		})
	}
	return out
}

func dataAt(committed []logstore.Entry, index int) string {
	if index < 1 || index > len(committed) {
		return ""
	}
	return committed[index-1].Data
}

// containsSubsequence 判断 got 是否按顺序包含全部 want（非连续匹配）。
// 脚本里通常用空格分隔精确序列，这里允许中间插入其他条目。
func containsSubsequence(got, want []string) bool {
	if len(want) == 0 {
		return true
	}
	j := 0
	for _, g := range got {
		if g == want[j] {
			j++
			if j == len(want) {
				return true
			}
		}
	}
	return false
}
