package twopc

import (
	"strings"

	"twopc-sim/internal/engine"
	"twopc-sim/internal/wal"
)

// RetryTicks is the retransmission / query interval used by both roles.
const RetryTicks int64 = 8

// Participant implements the 2PC participant role.
//
// Safety rule implemented here: after the "prepared" record is durable the
// participant may ONLY wait for the coordinator's decision or poll it with
// QUERY. There is deliberately NO timer that aborts a prepared transaction:
// doing so would risk aborting while the coordinator committed, i.e. a
// partial commit. With the coordinator unavailable the participant blocks —
// this is inherent to blocking 2PC and shown explicitly by the tests.
type Participant struct {
	engine.Base
	eng     *engine.Engine
	logPath string
	coord   engine.NodeID
	log     *wal.Log

	// volatile state (wiped on crash, rebuilt from WAL on restart)
	kv       map[string]string
	prepared map[string][]wal.KVWrite
	done     map[string]string // "commit" | "abort"
	votedNo  map[string]bool
	querying map[string]bool
}

// NewParticipant constructs a participant with an initial committed KV state.
func NewParticipant(eng *engine.Engine, id engine.NodeID, coord engine.NodeID, dataDir string, initialKV map[string]string) (*Participant, error) {
	lg, err := wal.Open(dataDir, string(id))
	if err != nil {
		return nil, err
	}
	p := &Participant{
		eng:      eng,
		logPath:  dataDir,
		coord:    coord,
		log:      lg,
		kv:       map[string]string{},
		prepared: map[string][]wal.KVWrite{},
		done:     map[string]string{},
		votedNo:  map[string]bool{},
		querying: map[string]bool{},
	}
	for k, v := range initialKV {
		p.kv[k] = v
	}
	p.Base.NodeIDValue = id
	return p, nil
}

// KV returns the volatile application state (after WAL replay on restart).
func (p *Participant) KV() map[string]string { return p.kv }

// Crash simulates process death: close the file and wipe all memory.
func (p *Participant) Crash() {
	if p.log != nil {
		_ = p.log.Close()
		p.log = nil
	}
	p.kv = nil
	p.prepared = nil
	p.done = nil
	p.votedNo = nil
	p.querying = nil
	p.MarkDown()
}

// OnRestart rebuilds state solely from the durable log.
func (p *Participant) OnRestart(now int64) {
	lg, err := wal.Open(p.logPath, string(p.ID()))
	if err != nil {
		panic(err)
	}
	p.log = lg
	p.kv = map[string]string{}
	p.prepared = map[string][]wal.KVWrite{}
	p.done = map[string]string{}
	p.querying = map[string]bool{}
	recs, err := wal.ReadAll(p.logPath, string(p.ID()))
	if err != nil {
		panic(err)
	}
	for _, r := range recs {
		switch r.Kind {
		case "prepared":
			p.prepared[r.TxnID] = r.Writes
		case "commit":
			delete(p.prepared, r.TxnID)
			p.done[r.TxnID] = "commit"
			for _, w := range r.Writes {
				p.kv[w.Key] = w.Value
			}
		case "abort":
			delete(p.prepared, r.TxnID)
			p.done[r.TxnID] = "abort"
		}
	}
	p.MarkUp()
	// Every recovered in-doubt transaction blocks on the decision and polls.
	for txn := range p.prepared {
		t := txn
		// Milestone hook: a test may crash again exactly at recovery.
		p.CrashAt("p-recovery-prepared:" + t)
		p.startQueryLoop(t)
	}
}

// votePolicy decides the local yes/no vote. It exists so scenarios can force
// a NO without any special protocol plumbing: a key prefixed "!" or the
// sentinel value @@NO makes the participant vote NO.
func votePolicy(writes []wal.KVWrite) bool {
	for _, w := range writes {
		if strings.HasPrefix(w.Key, "!") || w.Value == "@@NO" {
			return false
		}
	}
	return true
}

func (p *Participant) sendYes(txn string, writes []wal.KVWrite) {
	p.eng.Send(p.ID(), p.coord, Vote{TxnID: txn, Yes: true})
	p.CrashAt("p-vote-sent:" + txn)
}

// startQueryLoop retransmits QUERY until a decision arrives. It never times
// out into an abort — that is the whole point.
func (p *Participant) startQueryLoop(txn string) {
	if p.querying[txn] {
		return
	}
	p.querying[txn] = true
	var tick func(now int64)
	tick = func(now int64) {
		if !p.querying[txn] {
			return
		}
		p.eng.Send(p.ID(), p.coord, Query{TxnID: txn})
		engine.After(p.eng, &p.Base, RetryTicks, tick)
	}
	engine.After(p.eng, &p.Base, RetryTicks, tick)
}

func (p *Participant) stopQuery(txn string) { delete(p.querying, txn) }

func (p *Participant) applyCommit(txn string, writes []wal.KVWrite) {
	for _, w := range writes {
		p.kv[w.Key] = w.Value
	}
	p.done[txn] = "commit"
	delete(p.prepared, txn)
	p.stopQuery(txn)
}

func (p *Participant) applyAbort(txn string) {
	p.done[txn] = "abort"
	delete(p.prepared, txn)
	p.stopQuery(txn)
}

// HandleMessage dispatches one network message.
func (p *Participant) HandleMessage(now int64, from engine.NodeID, payload any) {
	switch m := payload.(type) {
	case Prepare:
		p.onPrepare(m)
	case Commit:
		p.onCommit(m)
	case Abort:
		p.onAbort(m)
	case QueryReply:
		p.onQueryReply(m)
	}
}

func (p *Participant) onPrepare(m Prepare) {
	// Crash window: PREPARE received, nothing durable yet.
	p.CrashAt("p-prepare-recv:" + m.TxnID)

	switch p.done[m.TxnID] {
	case "commit":
		// Duplicate PREPARE for an already decided txn: re-affirm so a
		// recovering coordinator converges to the same outcome.
		p.eng.Send(p.ID(), p.coord, Vote{TxnID: m.TxnID, Yes: true})
		return
	case "abort":
		p.eng.Send(p.ID(), p.coord, Vote{TxnID: m.TxnID, Yes: false})
		return
	}
	if _, ok := p.prepared[m.TxnID]; ok {
		// Duplicate PREPARE: resend the durable YES vote.
		p.eng.Send(p.ID(), p.coord, Vote{TxnID: m.TxnID, Yes: true})
		return
	}
	if !votePolicy(m.Writes) {
		p.votedNo[m.TxnID] = true
		p.eng.Send(p.ID(), p.coord, Vote{TxnID: m.TxnID, Yes: false})
		return
	}
	// Durable prepare BEFORE the vote: if we crash after this point we must
	// honor whatever decision the coordinator reaches.
	if err := p.log.Append(wal.Record{Kind: "prepared", TxnID: m.TxnID, Writes: m.Writes}); err != nil {
		panic(err)
	}
	durable(p.eng, p.ID(), "prepared", m.TxnID)
	p.prepared[m.TxnID] = m.Writes
	p.CrashAt("p-prepared-wal:" + m.TxnID)
	p.sendYes(m.TxnID, m.Writes)
	p.startQueryLoop(m.TxnID)
}

func (p *Participant) onCommit(m Commit) {
	p.CrashAt("p-commit-recv:" + m.TxnID)
	if p.done[m.TxnID] == "commit" {
		p.eng.Send(p.ID(), p.coord, Ack{TxnID: m.TxnID}) // idempotent
		return
	}
	if p.done[m.TxnID] == "abort" {
		// Logically impossible in correct 2PC; still ack to stop retransmission.
		p.eng.Send(p.ID(), p.coord, Ack{TxnID: m.TxnID})
		return
	}
	writes, ok := p.prepared[m.TxnID]
	if !ok {
		// COMMIT for a transaction we never prepared durably: cannot apply.
		p.eng.Send(p.ID(), p.coord, Ack{TxnID: m.TxnID})
		return
	}
	_ = writes
	if err := p.log.Append(wal.Record{Kind: "commit", TxnID: m.TxnID, Writes: m.Writes}); err != nil {
		panic(err)
	}
	durable(p.eng, p.ID(), "commit", m.TxnID)
	p.CrashAt("p-commit-wal:" + m.TxnID)
	p.applyCommit(m.TxnID, m.Writes)
	p.eng.Send(p.ID(), p.coord, Ack{TxnID: m.TxnID})
}

func (p *Participant) onAbort(m Abort) {
	if p.done[m.TxnID] != "" {
		p.eng.Send(p.ID(), p.coord, Ack{TxnID: m.TxnID})
		return
	}
	if err := p.log.Append(wal.Record{Kind: "abort", TxnID: m.TxnID}); err != nil {
		panic(err)
	}
	durable(p.eng, p.ID(), "abort", m.TxnID)
	p.CrashAt("p-abort-wal:" + m.TxnID)
	p.applyAbort(m.TxnID)
	p.eng.Send(p.ID(), p.coord, Ack{TxnID: m.TxnID})
}

func (p *Participant) onQueryReply(m QueryReply) {
	if !m.Known {
		return // decision still unknown: keep blocking and polling
	}
	if m.Commit {
		if p.done[m.TxnID] == "commit" {
			return
		}
		p.onCommit(Commit{TxnID: m.TxnID, Writes: m.Writes})
	} else {
		if p.done[m.TxnID] == "abort" {
			return
		}
		p.onAbort(Abort{TxnID: m.TxnID})
	}
}
