package twopc

import (
	"twopc-sim/internal/engine"
	"twopc-sim/internal/wal"
)

// txnState is the coordinator's volatile per-transaction state.
type txnState struct {
	writes  []wal.KVWrite
	members []engine.NodeID
	votes   map[engine.NodeID]bool // vote value (true=yes)
	acks    map[engine.NodeID]bool
	decided bool
	commit  bool
	done    bool // all acks received, client notified
}

// Coordinator implements the 2PC coordinator role.
type Coordinator struct {
	engine.Base
	eng          *engine.Engine
	logPath      string
	log          *wal.Log
	members      []engine.NodeID
	txns         map[string]*txnState
	clientResult map[string]string // txn -> "committed" | "aborted"
}

// NewCoordinator constructs the coordinator.
func NewCoordinator(eng *engine.Engine, id engine.NodeID, members []engine.NodeID, dataDir string) (*Coordinator, error) {
	lg, err := wal.Open(dataDir, string(id))
	if err != nil {
		return nil, err
	}
	c := &Coordinator{
		eng:          eng,
		logPath:      dataDir,
		log:          lg,
		members:      members,
		txns:         map[string]*txnState{},
		clientResult: map[string]string{},
	}
	c.Base.NodeIDValue = id
	return c, nil
}

// ClientOutcome returns the outcome reported to the client ("committed" /
// "aborted") once all participants acknowledged; missing means the client is
// still waiting at the end of the run.
func (c *Coordinator) ClientOutcome(txn string) (string, bool) {
	v, ok := c.clientResult[txn]
	return v, ok
}

// Crash simulates process death: the log survives, all memory is wiped.
func (c *Coordinator) Crash() {
	if c.log != nil {
		_ = c.log.Close()
		c.log = nil
	}
	c.txns = nil
	c.clientResult = nil
	c.MarkDown()
}

// OnRestart rebuilds state from the WAL. Undecided transactions are aborted
// (presumed-abort): because "begin" is durable before PREPARE is ever sent
// and the decision is durable before COMMIT/ABORT is sent, a transaction
// found undecided in the log was never decided anywhere, so abort is safe.
func (c *Coordinator) OnRestart(now int64) {
	lg, err := wal.Open(c.logPath, string(c.ID()))
	if err != nil {
		panic(err)
	}
	c.log = lg
	c.txns = map[string]*txnState{}
	c.clientResult = map[string]string{}
	recs, err := wal.ReadAll(c.logPath, string(c.ID()))
	if err != nil {
		panic(err)
	}
	for _, r := range recs {
		st, ok := c.txns[r.TxnID]
		if !ok {
			members := c.members
			if len(r.Participants) > 0 {
				members = toNodeIDs(r.Participants)
			}
			st = &txnState{
				members: members,
				writes:  r.Writes,
				votes:   map[engine.NodeID]bool{},
				acks:    map[engine.NodeID]bool{},
			}
			c.txns[r.TxnID] = st
		}
		switch r.Kind {
		case "begin":
			// state already populated
		case "commit":
			st.decided, st.commit = true, true
		case "abort":
			st.decided, st.commit = true, false
		}
	}
	c.MarkUp()

	// Recovery protocol.
	for txn, st := range c.txns {
		if st.decided {
			// Decision is durable: re-drive it to every participant.
			c.CrashAt("c-recovery-decision:" + txn)
			c.broadcastDecision(txn, st)
			c.startTicker(txn)
			continue
		}
		// Undecided at restart: no decision message was ever sent, abort.
		if err := c.log.Append(wal.Record{Kind: "abort", TxnID: txn}); err != nil {
			panic(err)
		}
		durable(c.eng, c.ID(), "abort", txn)
		st.decided, st.commit = true, false
		c.CrashAt("c-recovery-abort-wal:" + txn)
		c.broadcastDecision(txn, st)
		c.startTicker(txn)
	}
}

func toNodeIDs(ss []string) []engine.NodeID {
	out := make([]engine.NodeID, len(ss))
	for i, s := range ss {
		out[i] = engine.NodeID(s)
	}
	return out
}

// Begin starts one transaction (simulated client request).
func (c *Coordinator) Begin(txn string, writes []wal.KVWrite) {
	st := &txnState{
		writes:  writes,
		members: c.members,
		votes:   map[engine.NodeID]bool{},
		acks:    map[engine.NodeID]bool{},
	}
	// Participant list and writes are durable before any PREPARE leaves.
	if err := c.log.Append(wal.Record{
		Kind:         "begin",
		TxnID:        txn,
		Participants: toStrings(c.members),
		Writes:       writes,
	}); err != nil {
		panic(err)
	}
	durable(c.eng, c.ID(), "begin", txn)
	c.txns[txn] = st
	c.CrashAt("c-begin-wal:" + txn)

	for _, p := range c.members {
		c.eng.Send(c.ID(), p, Prepare{TxnID: txn, Writes: writes})
	}
	c.CrashAt("c-prepare-sent:" + txn)
	c.startTicker(txn)
}

func toStrings(ns []engine.NodeID) []string {
	out := make([]string, len(ns))
	for i, n := range ns {
		out[i] = string(n)
	}
	return out
}

// startTicker drives retransmission for one transaction until it is done.
func (c *Coordinator) startTicker(txn string) {
	var tick func(now int64)
	tick = func(now int64) {
		st, ok := c.txns[txn]
		if !ok || st.done {
			return
		}
		if !st.decided {
			for _, p := range st.members {
				if _, has := st.votes[p]; !has {
					c.eng.Send(c.ID(), p, Prepare{TxnID: txn, Writes: st.writes})
				}
			}
		} else {
			c.broadcastDecision(txn, st)
		}
		engine.After(c.eng, &c.Base, RetryTicks, tick)
	}
	engine.After(c.eng, &c.Base, RetryTicks, tick)
}

func (c *Coordinator) broadcastDecision(txn string, st *txnState) {
	if st.commit {
		c.eng.Trace("decision-send", string(c.ID()), "txn="+txn+" decision=commit")
		for _, p := range st.members {
			if !st.acks[p] {
				c.eng.Send(c.ID(), p, Commit{TxnID: txn, Writes: st.writes})
			}
		}
	} else {
		c.eng.Trace("decision-send", string(c.ID()), "txn="+txn+" decision=abort")
		for _, p := range st.members {
			if !st.acks[p] {
				c.eng.Send(c.ID(), p, Abort{TxnID: txn})
			}
		}
	}
}

// HandleMessage dispatches one network message.
func (c *Coordinator) HandleMessage(now int64, from engine.NodeID, payload any) {
	switch m := payload.(type) {
	case Vote:
		c.onVote(m, from)
	case Ack:
		c.onAck(m, from)
	case Query:
		c.onQuery(m, from)
	}
}

func (c *Coordinator) onVote(m Vote, from engine.NodeID) {
	st, ok := c.txns[m.TxnID]
	if !ok || st.decided {
		return
	}
	if _, seen := st.votes[from]; seen {
		return // duplicate vote
	}
	st.votes[from] = m.Yes
	c.CrashAt("c-vote-recv:" + m.TxnID)
	if len(st.votes) == len(st.members) {
		allYes := true
		for _, v := range st.votes {
			allYes = allYes && v
		}
		// Crash window after the vote set is complete, before the durable
		// decision is written.
		c.CrashAt("c-votes-complete:" + m.TxnID)
		rec := "abort"
		if allYes {
			rec = "commit"
		}
		if err := c.log.Append(wal.Record{Kind: rec, TxnID: m.TxnID}); err != nil {
			panic(err)
		}
		durable(c.eng, c.ID(), rec, m.TxnID)
		st.decided, st.commit = true, allYes
		// Crash window after the decision is durable, before it is sent.
		c.CrashAt("c-decision-wal:" + m.TxnID)
		c.broadcastDecision(m.TxnID, st)
	}
}

func (c *Coordinator) onAck(m Ack, from engine.NodeID) {
	st, ok := c.txns[m.TxnID]
	if !ok || !st.decided {
		return
	}
	if !st.acks[from] {
		st.acks[from] = true
		c.CrashAt("c-ack-recv:" + m.TxnID)
	}
	if len(st.acks) == len(st.members) && !st.done {
		st.done = true
		outcome := "aborted"
		if st.commit {
			outcome = "committed"
		}
		c.CrashAt("c-all-acked:" + m.TxnID)
		c.clientResult[m.TxnID] = outcome
	}
}

func (c *Coordinator) onQuery(m Query, from engine.NodeID) {
	st, ok := c.txns[m.TxnID]
	reply := QueryReply{TxnID: m.TxnID}
	if ok && st.decided {
		reply.Known = true
		reply.Commit = st.commit
		if st.commit {
			reply.Writes = st.writes
		}
	}
	c.eng.Send(c.ID(), from, reply)
}
