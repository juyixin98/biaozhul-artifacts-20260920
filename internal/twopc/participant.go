package twopc

import (
	"encoding/json"

	"twopcsim/internal/wal"
)

// 参与者 WAL 记录类型。
const (
	recPartPrepared  = "PART_PREPARED"  // {txnId} 准备就绪（持锁，不得自行回滚）
	recPartCommitted = "PART_COMMITTED" // {txnId} 已提交
	recPartAborted   = "PART_ABORTED"   // {txnId} 已中止
)

type partTxn struct {
	state     string // prepared | committed | aborted
	lockHeld  bool
	sinceTick int64
}

// Participant 是 2PC 参与者（模拟本地资源管理器）。
type Participant struct {
	id       string
	coordID  string
	peers    []string // 另外两个参与者（终止协议询问对象）
	timings  Timings
	log      *wal.WAL
	ctx      Context
	txns     map[string]*partTxn
	voteNo   map[string]bool // txnID -> 本地投否决（场景可配）
	querying map[string]bool // 防止重复安排询问定时器
}

// NewParticipant 创建参与者。voteNoTxns 中的事务本地拒绝提交。
func NewParticipant(id, coordID string, peers []string, timings Timings, log *wal.WAL, voteNoTxns []string) *Participant {
	voteNo := map[string]bool{}
	for _, t := range voteNoTxns {
		voteNo[t] = true
	}
	return &Participant{
		id:       id,
		coordID:  coordID,
		peers:    append([]string(nil), peers...),
		timings:  timings,
		log:      log,
		txns:     map[string]*partTxn{},
		voteNo:   voteNo,
		querying: map[string]bool{},
	}
}

func (p *Participant) Bind(ctx Context) { p.ctx = ctx }

func (p *Participant) ID() string { return p.id }

// Begin 对参与者无意义（事务由协调者发起）。
func (p *Participant) Begin(string) {}

func (p *Participant) OnMessage(m Message) {
	switch m.Type {
	case MsgPrepare:
		p.onPrepare(m)
	case MsgGlobalCommit:
		p.onGlobalDecision(m, true)
	case MsgGlobalAbort:
		p.onGlobalDecision(m, false)
	case MsgDecisionRequest:
		p.onDecisionRequest(m)
	case MsgDecisionCommit:
		p.onPeerDecision(m, true)
	case MsgDecisionAbort:
		p.onPeerDecision(m, false)
	}
}

// onPrepare 处理第一阶段：本地决定后先写 WAL 再回复。
func (p *Participant) onPrepare(m Message) {
	if t := p.txns[m.TxnID]; t != nil {
		// 幂等：重发的 PREPARE。已 PREPARED 的重新投票；终态不再响应。
		switch t.state {
		case StatePrepared:
			p.ctx.Send(Message{Type: MsgVoteCommit, TxnID: m.TxnID, From: p.id, To: m.From})
			p.ctx.Event("participant.prepare.duplicate", m.TxnID, map[string]any{"action": "revote-commit"})
		case StateCommitted:
			// 协调者显然错过了决议结果，直接补发 ACK 帮助其收敛。
			p.ctx.Send(Message{Type: MsgAck, TxnID: m.TxnID, From: p.id, To: m.From})
		}
		return
	}

	// 本地资源检查（模拟）：场景配置为否决时拒绝。
	if p.voteNo[m.TxnID] {
		p.ctx.Event("participant.localAbort", m.TxnID, map[string]any{"reason": "vote-no"})
		// 投否决前可以先写 ABORTED（崩溃恢复后仍知道自己已单方中止）。
		if p.ctx.Hook(HookPartBeforeAbort, m.TxnID) {
			return
		}
		p.writeTerminal(m.TxnID, recPartAborted, StateAborted, false)
		if p.ctx.Hook(HookPartAfterAbort, m.TxnID) {
			return
		}
		p.ctx.Send(Message{Type: MsgVoteAbort, TxnID: m.TxnID, From: p.id, To: m.From})
		return
	}

	if p.ctx.Hook(HookPartBeforePrepared, m.TxnID) {
		return
	}
	if err := p.log.Append(recPartPrepared, txnRec{TxnID: m.TxnID}); err != nil {
		panic(err)
	}
	pt := &partTxn{state: StatePrepared, lockHeld: true, sinceTick: p.ctx.Now()}
	p.txns[m.TxnID] = pt
	p.ctx.Event("participant.prepared.fsynced", m.TxnID, map[string]any{"lockHeld": true})

	// 协议保证：PREPARED 之后绝不自行超时回滚。
	if p.ctx.Hook(HookPartAfterPrepared, m.TxnID) {
		return
	}
	if p.ctx.Hook(HookPartBeforeVote, m.TxnID) {
		return
	}
	p.ctx.Send(Message{Type: MsgVoteCommit, TxnID: m.TxnID, From: p.id, To: m.From})
	p.ctx.Event("participant.voteCommit.sent", m.TxnID, nil)

	// 开始周期性询问决议（不是回滚定时器！）。
	p.startQuerying(m.TxnID)
}

// onGlobalDecision 处理第二阶段决议：幂等，先 fsync 再 ACK。
func (p *Participant) onGlobalDecision(m Message, commit bool) {
	t := p.txns[m.TxnID]
	if t != nil {
		// 终态收到重发：仅补 ACK。
		if (commit && t.state == StateCommitted) || (!commit && t.state == StateAborted) {
			p.ctx.Send(Message{Type: MsgAck, TxnID: m.TxnID, From: p.id, To: m.From})
			return
		}
		if !commit && t.state == StatePrepared {
			// 合法路径
		} else if commit && t.state != StatePrepared {
			p.ctx.Event("participant.protocolViolation", m.TxnID, map[string]any{
				"reason": "commit without prepared", "state": t.state,
			})
		}
	}
	// t == nil 且收到 GLOBAL_ABORT：可能是 PREPARE 丢失或节点曾在无记录时崩溃，
	// 中止总是安全的，补写 ABORTED 并 ACK。
	if t == nil && commit {
		p.ctx.Event("participant.protocolViolation", m.TxnID, map[string]any{
			"reason": "commit for unknown txn",
		})
		// 仍按协调者决议执行（协调者日志是唯一权威），但留下冲突证据。
	}

	if commit {
		if p.ctx.Hook(HookPartBeforeCommit, m.TxnID) {
			return
		}
		p.writeTerminal(m.TxnID, recPartCommitted, StateCommitted, false)
		if p.ctx.Hook(HookPartAfterCommit, m.TxnID) {
			return
		}
	} else {
		if p.ctx.Hook(HookPartBeforeAbort, m.TxnID) {
			return
		}
		p.writeTerminal(m.TxnID, recPartAborted, StateAborted, false)
		if p.ctx.Hook(HookPartAfterAbort, m.TxnID) {
			return
		}
	}
	delete(p.querying, m.TxnID) // 决议已达，停止询问
	p.ctx.Send(Message{Type: MsgAck, TxnID: m.TxnID, From: p.id, To: m.From})
	p.ctx.Event("participant.ack.sent", m.TxnID, map[string]any{"decision": m.Type})
}

func (p *Participant) writeTerminal(txnID, recType, state string, lock bool) {
	if err := p.log.Append(recType, txnRec{TxnID: txnID}); err != nil {
		panic(err)
	}
	if t := p.txns[txnID]; t != nil {
		t.state = state
		t.lockHeld = lock
	} else {
		p.txns[txnID] = &partTxn{state: state, lockHeld: lock, sinceTick: p.ctx.Now()}
	}
}

// startQuerying 安排周期性决议询问。PREPARED 后参与者只能等——
// 没有协调者（或持决议的同伴）答复前，锁一直持有，绝不中止。
func (p *Participant) startQuerying(txnID string) {
	if p.querying[txnID] {
		return
	}
	p.querying[txnID] = true
	p.ctx.Timer(TimerQuery, txnID, p.timings.Query)
}

// onDecisionRequest 处理同伴询问：只有自己处于确定状态时才能答复。
func (p *Participant) onDecisionRequest(m Message) {
	t := p.txns[m.TxnID]
	if t == nil {
		// 自己没有任何记录 => 从未投过赞成票，事务中止是安全的。
		p.ctx.Send(Message{Type: MsgDecisionAbort, TxnID: m.TxnID, From: p.id, To: m.From})
		return
	}
	switch t.state {
	case StateCommitted:
		p.ctx.Send(Message{Type: MsgDecisionCommit, TxnID: m.TxnID, From: p.id, To: m.From})
	case StateAborted:
		p.ctx.Send(Message{Type: MsgDecisionAbort, TxnID: m.TxnID, From: p.id, To: m.From})
	case StatePrepared:
		// 同样在等决议：无法帮助对方，保持沉默（对方继续阻塞）。
		p.ctx.Event("participant.query.peerWait", m.TxnID, map[string]any{"from": m.From})
	}
}

// onPeerDecision 处理终止协议答复。
func (p *Participant) onPeerDecision(m Message, commit bool) {
	t := p.txns[m.TxnID]
	// 只有处于 PREPARED 的询问者才需要按答复行动。
	if t == nil || t.state != StatePrepared {
		return
	}
	if commit {
		p.writeTerminal(m.TxnID, recPartCommitted, StateCommitted, false)
		p.ctx.Event("participant.commit.fromPeer", m.TxnID, map[string]any{"from": m.From})
		// 同伴决议也视为终态；协调者恢复后通过重发 PREPARE 或 ACK 交换收敛。
		p.ctx.Send(Message{Type: MsgAck, TxnID: m.TxnID, From: p.id, To: p.coordID})
	} else {
		p.writeTerminal(m.TxnID, recPartAborted, StateAborted, false)
		p.ctx.Event("participant.abort.fromPeer", m.TxnID, map[string]any{"from": m.From})
		p.ctx.Send(Message{Type: MsgAck, TxnID: m.TxnID, From: p.id, To: p.coordID})
	}
	delete(p.querying, m.TxnID)
}

// OnTimer 处理询问定时器：向协调者与所有同伴发 DECISION_REQUEST。
func (p *Participant) OnTimer(kind, txnID string) {
	if kind != TimerQuery {
		return
	}
	t := p.txns[txnID]
	if t == nil || t.state != StatePrepared {
		delete(p.querying, txnID)
		return
	}
	// 仍处于 PREPARED：继续等，继续问。这不是超时回滚。
	p.ctx.Event("participant.query.tick", txnID, map[string]any{
		"waitedTicks": p.ctx.Now() - t.sinceTick, "lockHeld": true,
	})
	p.ctx.Send(Message{Type: MsgDecisionRequest, TxnID: txnID, From: p.id, To: p.coordID})
	for _, peer := range p.peers {
		p.ctx.Send(Message{Type: MsgDecisionRequest, TxnID: txnID, From: p.id, To: peer})
	}
	p.ctx.Timer(TimerQuery, txnID, p.timings.Query)
}

// Recover 重放 WAL；PREPARED 事务恢复后重新持锁并继续等待决议。
func (p *Participant) Recover() {
	p.txns = map[string]*partTxn{}
	err := p.log.Replay(func(r wal.Record) error {
		var d txnRec
		if err := json.Unmarshal(r.Data, &d); err != nil {
			return err
		}
		switch r.Type {
		case recPartPrepared:
			// 可能后面还有同事务的终态记录，先标记，后续覆盖。
			p.txns[d.TxnID] = &partTxn{state: StatePrepared, lockHeld: true, sinceTick: p.ctx.Now()}
		case recPartCommitted:
			p.txns[d.TxnID] = &partTxn{state: StateCommitted, lockHeld: false, sinceTick: p.ctx.Now()}
		case recPartAborted:
			p.txns[d.TxnID] = &partTxn{state: StateAborted, lockHeld: false, sinceTick: p.ctx.Now()}
		}
		return nil
	})
	if err != nil {
		panic(err)
	}
	for id, t := range p.txns {
		if t.state == StatePrepared {
			p.ctx.Event("participant.recover.prepared", id, map[string]any{"lockHeld": true})
			p.startQuerying(id)
		} else {
			p.ctx.Event("participant.recover.terminal", id, map[string]any{"state": t.state})
		}
	}
}

// Snapshot 输出参与者当前状态。
func (p *Participant) Snapshot() NodeSnapshot {
	states := map[string]TxnView{}
	for id, t := range p.txns {
		states[id] = TxnView{State: t.state, LockHeld: t.lockHeld, SinceTick: t.sinceTick}
	}
	return NodeSnapshot{ID: p.id, Role: "participant", TxnStates: states}
}

func (p *Participant) Close() error { return p.log.Close() }
