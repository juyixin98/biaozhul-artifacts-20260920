package twopc

import (
	"encoding/json"

	"twopcsim/internal/wal"
)

// 协调者 WAL 记录类型。
const (
	recCoordStart  = "COORD_START"  // {txnId, participants}
	recCoordCommit = "COORD_COMMIT" // {txnId}
	recCoordAbort  = "COORD_ABORT"  // {txnId}
)

// Timings 控制协议定时器（tick 数），便于按场景调大观察窗口。
type Timings struct {
	VoteTimeout int64 `json:"voteTimeout"` // 收齐投票截止时间
	Resend      int64 `json:"resend"`      // 第二阶段决议重发间隔
	Query       int64 `json:"query"`       // 参与者询问间隔
}

// DefaultTimings 为默认定时器配置。
func DefaultTimings() Timings {
	return Timings{VoteTimeout: 40, Resend: 15, Query: 10}
}

type startRec struct {
	TxnID        string   `json:"txnId"`
	Participants []string `json:"participants"`
}

type txnRec struct {
	TxnID string `json:"txnId"`
}

type coordTxn struct {
	id         string
	state      string // preparing | committing | aborting | committed | aborted
	parts      []string
	yesVotes   map[string]bool
	acked      map[string]bool
	gotVotes   map[string]bool
	sinceTick  int64
	commitTick int64 // 决议形成时刻（阻塞时长统计从这里无意义，仅记录）
}

// Coordinator 是 2PC 协调者。
type Coordinator struct {
	id       string
	timings  Timings
	log      *wal.WAL
	ctx      Context
	txns     map[string]*coordTxn
	topology []string // 固定参与者列表，由 runner 注入
}

// NewCoordinator 创建协调者（打开/创建其 WAL，不做重放——首次运行）。
func NewCoordinator(id string, log *wal.WAL, timings Timings) *Coordinator {
	return &Coordinator{
		id:      id,
		timings: timings,
		log:     log,
		txns:    map[string]*coordTxn{},
	}
}

// Bind 注入运行上下文（runner 在构造网络后调用）。
func (c *Coordinator) Bind(ctx Context) { c.ctx = ctx }

func (c *Coordinator) ID() string { return c.id }

// Begin 开启一个事务：fsync START 后进入第一阶段。
func (c *Coordinator) Begin(txnID string) {
	if _, ok := c.txns[txnID]; ok {
		return
	}
	t := &coordTxn{
		id:        txnID,
		state:     StatePreparing,
		yesVotes:  map[string]bool{},
		acked:     map[string]bool{},
		gotVotes:  map[string]bool{},
		sinceTick: c.ctx.Now(),
	}
	c.txns[txnID] = t

	t.parts = append([]string(nil), c.topology...)
	if err := c.log.Append(recCoordStart, startRec{TxnID: txnID, Participants: t.parts}); err != nil {
		panic(err)
	}
	c.ctx.Event("coord.start.fsynced", txnID, map[string]any{"participants": t.parts})
	if c.ctx.Hook(HookCoordStartAppended, txnID) {
		return
	}
	if c.ctx.Hook(HookCoordBeforePrepares, txnID) {
		return
	}
	c.sendPrepares(t)
	c.ctx.Timer(TimerVoteTimeout, txnID, c.timings.VoteTimeout)
	// 准备阶段周期性重发 PREPARE（对抗丢包），直到收齐投票或超时。
	c.ctx.Timer(TimerResend, txnID, c.timings.Resend)
}

// topology 由 runner 通过 SetTopology 注入（固定 3 个参与者）。

func (c *Coordinator) sendPrepares(t *coordTxn) {
	for _, p := range t.parts {
		c.ctx.Send(Message{Type: MsgPrepare, TxnID: t.id, From: c.id, To: p})
	}
	c.ctx.Event("coord.prepare.sent", t.id, map[string]any{"to": t.parts})
}

func (c *Coordinator) OnMessage(m Message) {
	switch m.Type {
	case MsgVoteCommit, MsgVoteAbort:
		c.onVote(m)
	case MsgAck:
		c.onAck(m)
	case MsgDecisionRequest:
		c.onDecisionRequest(m)
	}
}

func (c *Coordinator) onVote(m Message) {
	t := c.txns[m.TxnID]
	if t == nil {
		// 崩溃恢复后 START 已重放，理论上不会为 nil；防御性处理：忽略。
		return
	}
	// 决议已定：投票迟到，忽略（幂等网络下的正常情况）。
	if t.state != StatePreparing {
		return
	}
	if t.gotVotes[m.From] {
		return // 重复投票
	}
	t.gotVotes[m.From] = true
	if m.Type == MsgVoteCommit {
		t.yesVotes[m.From] = true
	}
	c.ctx.Event("coord.vote", m.TxnID, map[string]any{
		"from": m.From, "vote": m.Type,
		"yes": len(t.yesVotes), "total": len(t.parts),
	})

	if len(t.yesVotes) == len(t.parts) {
		c.decideCommit(t)
		return
	}
	// 收到一张否决票：立即决定中止。
	if m.Type == MsgVoteAbort {
		c.decideAbort(t)
	}
}

func (c *Coordinator) decideCommit(t *coordTxn) {
	if err := c.log.Append(recCoordCommit, txnRec{TxnID: t.id}); err != nil {
		panic(err)
	}
	t.state = StateCommitting
	t.commitTick = c.ctx.Now()
	c.ctx.Event("coord.commit.fsynced", t.id, map[string]any{"tick": t.commitTick})
	// 关键注入点：提交点之后、通知之前崩溃 => 参与者可能阻塞。
	if c.ctx.Hook(HookCoordCommitAppended, t.id) {
		return
	}
	if c.ctx.Hook(HookCoordBeforeCommitMsg, t.id) {
		return
	}
	c.broadcastDecision(t, MsgGlobalCommit)
	c.ctx.Timer(TimerResend, t.id, c.timings.Resend)
}

func (c *Coordinator) decideAbort(t *coordTxn) {
	if err := c.log.Append(recCoordAbort, txnRec{TxnID: t.id}); err != nil {
		panic(err)
	}
	t.state = StateAborting
	c.ctx.Event("coord.abort.fsynced", t.id, nil)
	if c.ctx.Hook(HookCoordAbortAppended, t.id) {
		return
	}
	if c.ctx.Hook(HookCoordBeforeAbortMsg, t.id) {
		return
	}
	c.broadcastDecision(t, MsgGlobalAbort)
	c.ctx.Timer(TimerResend, t.id, c.timings.Resend)
}

func (c *Coordinator) broadcastDecision(t *coordTxn, msgType string) {
	for _, p := range t.parts {
		c.ctx.Send(Message{Type: msgType, TxnID: t.id, From: c.id, To: p})
	}
	c.ctx.Event("coord.decision.sent", t.id, map[string]any{"decision": msgType})
}

func (c *Coordinator) onAck(m Message) {
	t := c.txns[m.TxnID]
	if t == nil {
		return
	}
	if t.state != StateCommitting && t.state != StateAborting {
		return
	}
	if !t.acked[m.From] {
		t.acked[m.From] = true
		c.ctx.Event("coord.ack", m.TxnID, map[string]any{
			"from": m.From, "acks": len(t.acked), "total": len(t.parts),
		})
	}
}

// onDecisionRequest 是终止协议的协调者侧：如实告知持久化决议。
func (c *Coordinator) onDecisionRequest(m Message) {
	t := c.txns[m.TxnID]
	if t == nil {
		// 完全不认识的事务：安全方向是中止。
		c.ctx.Send(Message{Type: MsgDecisionAbort, TxnID: m.TxnID, From: c.id, To: m.From})
		return
	}
	switch t.state {
	case StateCommitting:
		c.ctx.Send(Message{Type: MsgDecisionCommit, TxnID: t.id, From: c.id, To: m.From})
	case StateAborting:
		c.ctx.Send(Message{Type: MsgDecisionAbort, TxnID: t.id, From: c.id, To: m.From})
	default:
		// 决议尚未形成，不能给出任何方向（此时询问者必须继续阻塞）。
		c.ctx.Event("coord.query.unanswered", t.id, map[string]any{"from": m.From})
	}
}

func (c *Coordinator) OnTimer(kind, txnID string) {
	t := c.txns[txnID]
	if t == nil {
		return
	}
	switch kind {
	case TimerVoteTimeout:
		if t.state == StatePreparing {
			c.ctx.Event("coord.voteTimeout", txnID, map[string]any{
				"yes": len(t.yesVotes), "total": len(t.parts),
			})
			c.decideAbort(t)
		}
	case TimerResend:
		switch t.state {
		case StatePreparing:
			// 准备阶段：向尚未投票的参与者重发 PREPARE。
			missing := 0
			for _, p := range t.parts {
				if !t.gotVotes[p] {
					missing++
					c.ctx.Send(Message{Type: MsgPrepare, TxnID: t.id, From: c.id, To: p})
				}
			}
			c.ctx.Event("coord.prepare.resend", t.id, map[string]any{"missing": missing})
			c.ctx.Timer(TimerResend, t.id, c.timings.Resend)
		case StateCommitting:
			c.resendMissing(t, MsgGlobalCommit)
		case StateAborting:
			c.resendMissing(t, MsgGlobalAbort)
		}
	}
}

func (c *Coordinator) resendMissing(t *coordTxn, msgType string) {
	missing := 0
	for _, p := range t.parts {
		if !t.acked[p] {
			missing++
			c.ctx.Send(Message{Type: msgType, TxnID: t.id, From: c.id, To: p})
		}
	}
	c.ctx.Event("coord.decision.resend", t.id, map[string]any{"decision": msgType, "missing": missing})
	c.ctx.Timer(TimerResend, t.id, c.timings.Resend)
}

// Recover 重放 WAL 恢复全部事务状态，并重启重发定时器。
func (c *Coordinator) Recover() {
	c.txns = map[string]*coordTxn{}
	var order []string
	err := c.log.Replay(func(r wal.Record) error {
		switch r.Type {
		case recCoordStart:
			var d startRec
			if err := json.Unmarshal(r.Data, &d); err != nil {
				return err
			}
			c.txns[d.TxnID] = &coordTxn{
				id:        d.TxnID,
				state:     StatePreparing,
				parts:     d.Participants,
				yesVotes:  map[string]bool{},
				acked:     map[string]bool{},
				gotVotes:  map[string]bool{},
				sinceTick: c.ctx.Now(),
			}
			order = append(order, d.TxnID)
		case recCoordCommit:
			var d txnRec
			if err := json.Unmarshal(r.Data, &d); err != nil {
				return err
			}
			if t := c.txns[d.TxnID]; t != nil {
				t.state = StateCommitting
				t.commitTick = c.ctx.Now()
			}
		case recCoordAbort:
			var d txnRec
			if err := json.Unmarshal(r.Data, &d); err != nil {
				return err
			}
			if t := c.txns[d.TxnID]; t != nil {
				t.state = StateAborting
			}
		}
		return nil
	})
	if err != nil {
		panic(err)
	}

	// 恢复动作：
	//  - committing/aborting：重发决议并安排重发定时器（无 ACK 记忆则全员重发）；
	//  - preparing（崩溃发生在 START 之后、决议之前）：重新发送 PREPARE。
	for _, id := range order {
		t := c.txns[id]
		switch t.state {
		case StateCommitting:
			c.ctx.Event("coord.recover", id, map[string]any{"state": StateCommitting})
			c.broadcastDecision(t, MsgGlobalCommit)
			c.ctx.Timer(TimerResend, id, c.timings.Resend)
		case StateAborting:
			c.ctx.Event("coord.recover", id, map[string]any{"state": StateAborting})
			c.broadcastDecision(t, MsgGlobalAbort)
			c.ctx.Timer(TimerResend, id, c.timings.Resend)
		case StatePreparing:
			c.ctx.Event("coord.recover", id, map[string]any{"state": StatePreparing})
			c.sendPrepares(t)
			c.ctx.Timer(TimerVoteTimeout, id, c.timings.VoteTimeout)
		}
	}
}

// Snapshot 输出协调者当前状态。
func (c *Coordinator) Snapshot() NodeSnapshot {
	states := map[string]TxnView{}
	for id, t := range c.txns {
		states[id] = TxnView{State: t.state, SinceTick: t.sinceTick}
	}
	return NodeSnapshot{ID: c.id, Role: "coordinator", TxnStates: states}
}

// topology 固定拓扑，由 runner 注入。
func (c *Coordinator) SetTopology(parts []string) {
	cp := make([]string, len(parts))
	copy(cp, parts)
	c.topology = cp
}

func (c *Coordinator) Close() error { return c.log.Close() }
