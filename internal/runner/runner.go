package runner

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"

	"twopcsim/internal/sim"
	"twopcsim/internal/twopc"
	"twopcsim/internal/wal"
)

// CoordinatorID 与参与者编号是固定拓扑（1 协调者 + 3 参与者）。
const CoordinatorID = "coordinator"

// ParticipantIDs 返回固定的 3 个参与者 ID。
func ParticipantIDs() []string { return []string{"participant-1", "participant-2", "participant-3"} }

// TraceEntry 是一条协议追踪事件（报告证据）。
type TraceEntry struct {
	Tick   int64          `json:"tick"`
	Node   string         `json:"node"`
	Kind   string         `json:"kind"`
	TxnID  string         `json:"txnId,omitempty"`
	Detail map[string]any `json:"detail,omitempty"`
}

type crashState struct {
	rule      CrashRule
	hits      int
	done      bool
	crashed   bool // 规则确实触发过崩溃
	restarted bool // 规则配置的重启确实发生过
}

// runnerCtx 把节点协议代码与调度器/网络/故障注入连接起来。
type runnerCtx struct {
	owner string
	r     *Runner
}

func (c *runnerCtx) Now() int64 { return c.r.sched.Now() }

func (c *runnerCtx) Send(m twopc.Message) { c.r.netSend(c.owner, m) }

func (c *runnerCtx) Timer(kind, txnID string, delay int64) {
	owner := c.owner
	c.r.sched.Schedule(owner, "timer:"+kind, delay, func() {
		if !c.r.isUp(owner) {
			return
		}
		c.r.nodes[owner].OnTimer(kind, txnID)
	})
}

func (c *runnerCtx) Event(kind, txnID string, detail map[string]any) {
	c.r.trace = append(c.r.trace, TraceEntry{
		Tick: c.r.sched.Now(), Node: c.owner, Kind: kind, TxnID: txnID, Detail: detail,
	})
}

func (c *runnerCtx) Hook(point, txnID string) bool {
	return c.r.hookFired(c.owner, point, txnID)
}

// Runner 是一次模拟运行。
type Runner struct {
	sc    Scenario
	sched *sim.Scheduler

	nodes  map[string]twopc.Node
	coords map[string]*twopc.Coordinator
	parts  map[string]*twopc.Participant
	walh   map[string]*wal.WAL
	ctxs   map[string]*runnerCtx

	down    map[string]bool
	crashes map[string]*crashState

	trace []TraceEntry
}

// New 构造一次运行（打开 WAL、创建节点，但不启动任何事件）。
func New(sc Scenario) (*Runner, error) {
	sc = withDefaults(sc)
	if err := validate(sc); err != nil {
		return nil, err
	}
	dir := sc.DataDir
	if dir == "" {
		var err error
		dir, err = os.MkdirTemp("", "twopc-")
		if err != nil {
			return nil, err
		}
		sc.DataDir = dir
	}
	fresh := true
	if sc.Fresh != nil {
		fresh = *sc.Fresh
	}
	if fresh {
		if err := os.RemoveAll(dir); err != nil {
			return nil, err
		}
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, err
		}
	} else if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}

	r := &Runner{
		sc:      sc,
		sched:   sim.New(sc.Seed),
		nodes:   map[string]twopc.Node{},
		coords:  map[string]*twopc.Coordinator{},
		parts:   map[string]*twopc.Participant{},
		walh:    map[string]*wal.WAL{},
		ctxs:    map[string]*runnerCtx{},
		down:    map[string]bool{},
		crashes: map[string]*crashState{},
	}

	// 协调者。
	cw, err := wal.Open(dir, CoordinatorID)
	if err != nil {
		return nil, err
	}
	coord := twopc.NewCoordinator(CoordinatorID, cw, sc.Timings)
	coord.SetTopology(ParticipantIDs())
	r.walh[CoordinatorID] = cw
	r.coords[CoordinatorID] = coord
	r.nodes[CoordinatorID] = coord

	// 参与者。
	peersOf := map[string][]string{}
	for _, id := range ParticipantIDs() {
		var peers []string
		for _, other := range ParticipantIDs() {
			if other != id {
				peers = append(peers, other)
			}
		}
		peersOf[id] = peers
	}
	for _, id := range ParticipantIDs() {
		pw, err := wal.Open(dir, id)
		if err != nil {
			return nil, err
		}
		var voteNo []string
		for _, t := range sc.Transactions {
			for _, v := range t.VoteNo {
				if v == id {
					voteNo = append(voteNo, t.ID)
				}
			}
		}
		p := twopc.NewParticipant(id, CoordinatorID, peersOf[id], sc.Timings, pw, voteNo)
		r.walh[id] = pw
		r.parts[id] = p
		r.nodes[id] = p
	}

	// 每个节点一个绑定 owner 的上下文。
	for id, n := range r.nodes {
		ctx := &runnerCtx{owner: id, r: r}
		r.ctxs[id] = ctx
		switch v := n.(type) {
		case *twopc.Coordinator:
			v.Bind(ctx)
		case *twopc.Participant:
			v.Bind(ctx)
		}
	}

	// 崩溃规则。
	for i := range sc.Crashes {
		cr := sc.Crashes[i]
		key := fmt.Sprintf("%d:%s:%s:%s:%d", i, cr.NodeID, cr.Hook, cr.TxnID, cr.AtTick)
		r.crashes[key] = &crashState{rule: cr}
	}

	// 跨进程启动即恢复：若数据目录保留了历史 WAL（resume），重放重建内存状态。
	// 全新目录下 WAL 为空，调用是无副作用的 no-op。
	for _, id := range append([]string{CoordinatorID}, ParticipantIDs()...) {
		r.nodes[id].Recover()
	}
	return r, nil
}

// Run 执行模拟直到观察窗口结束。
func (r *Runner) Run() {
	// Begin 事件。
	for _, t := range r.sc.Transactions {
		t := t
		r.sched.Schedule("system", "begin", t.BeginTick, func() {
			if r.isUp(CoordinatorID) {
				r.coords[CoordinatorID].Begin(t.ID)
			}
		})
	}
	// atTick 崩溃事件（重启也在此规则内调度）。
	for _, cs := range r.sortedCrashStates() {
		cs := cs
		if cs.rule.Hook != "" {
			continue
		}
		r.sched.Schedule("system", "crashAtTick", cs.rule.AtTick, func() {
			r.crashByRule(cs, "atTick:"+itoa(cs.rule.AtTick))
		})
	}

	r.sched.Run(r.sc.MaxTick)

	// 记录观察窗口结束时各节点存活情况（证据）。顺序固定，保证确定性。
	for _, id := range append([]string{CoordinatorID}, ParticipantIDs()...) {
		state := "up"
		if r.down[id] {
			state = "down"
		}
		r.trace = append(r.trace, TraceEntry{
			Tick: r.sc.MaxTick, Node: id, Kind: "endofwindow." + state,
		})
	}
}

func (r *Runner) sortedCrashStates() []*crashState {
	var out []*crashState
	for _, cs := range r.crashes {
		out = append(out, cs)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].rule.AtTick != out[j].rule.AtTick {
			return out[i].rule.AtTick < out[j].rule.AtTick
		}
		return out[i].rule.NodeID < out[j].rule.NodeID
	})
	return out
}

func (r *Runner) isUp(id string) bool { return !r.down[id] }

// crashByRule 按某条注入规则触发崩溃，并安排其重启。
func (r *Runner) crashByRule(cs *crashState, trigger string) {
	if r.down[cs.rule.NodeID] {
		return
	}
	cs.crashed = true
	r.crashNode(cs.rule.NodeID, trigger)
	if cs.rule.RestartTick > 0 {
		r.scheduleRestartByRule(cs)
	}
}

func (r *Runner) crashNode(id, where string) {
	if r.down[id] {
		return
	}
	r.down[id] = true
	// 崩溃 = 易失状态全部丢失：
	// 该节点待触发的定时器/发给它的消息，以及它已发出但尚在途的消息，一并消失。
	owned, inFlight := r.sched.CancelNode(id)
	r.trace = append(r.trace, TraceEntry{
		Tick:   r.sched.Now(),
		Node:   id,
		Kind:   "node.crash",
		Detail: map[string]any{"trigger": where, "cancelledOwnedEvents": owned, "cancelledInFlight": inFlight},
	})
}

func (r *Runner) scheduleRestartByRule(cs *crashState) {
	id := cs.rule.NodeID
	at := cs.rule.RestartTick
	delay := at - r.sched.Now()
	r.sched.Schedule("system", "restart", delay, func() {
		if !r.down[id] {
			return
		}
		r.down[id] = false
		cs.restarted = true
		r.trace = append(r.trace, TraceEntry{
			Tick: r.sched.Now(), Node: id, Kind: "node.restart",
			Detail: map[string]any{"wal": filepath.Base(r.walh[id].Path()), "restartAt": at},
		})
		// 重放持久日志，恢复协议状态。
		r.nodes[id].Recover()
	})
}

// hookFired 在节点协议钩子处被调用：匹配规则则让节点崩溃。
func (r *Runner) hookFired(owner, point, txnID string) bool {
	fired := false
	for _, cs := range r.crashes {
		if cs.done {
			continue
		}
		cr := cs.rule
		if cr.Hook == "" || cr.NodeID != owner || cr.Hook != point {
			continue
		}
		if cr.TxnID != "" && cr.TxnID != txnID {
			continue
		}
		cs.hits++
		want := cr.Occurrence
		if want <= 0 {
			want = 1
		}
		if cs.hits != want {
			continue
		}
		cs.done = true
		fired = true
		r.crashByRule(cs, "hook:"+point)
	}
	return fired
}

func itoa(i int64) string {
	return strconv.FormatInt(i, 10)
}

// Close 关闭所有节点的 WAL（报告生成之后调用）。
func (r *Runner) Close() {
	for _, h := range r.walh {
		_ = h.Close()
	}
}
