package sim

import (
	"fmt"
	"sort"

	"chmig/ring"
)

func clientID(raw string) string { return "client:" + raw }

type plannedOp struct {
	at    int64
	write bool
	idx   int // plan index, used in ReqID/RID
}

// inflight tracks one outstanding write or read.
type inflight struct {
	key       string
	reqID     string
	value     string
	write     bool
	attempts  int
	to        string // current target node (internal id)
	issueTick int64
	tid       int64
	// read bookkeeping
	readVer   int
	readVal   string
	readFound bool
	// readSeen counts distinct responders (by node id) for this read attempt;
	// a not-found is accepted only when every dual-read target answered
	// not-found, so a single lost old-owner answer can never surface as empty.
	readSeen  map[string]bool
	readWant  int
	readStale int // answers rejected for being below the read watermark
}

type client struct {
	cfg ClientCfg
	aid string
	eng *Engine
	w   *world

	current *ring.Ring
	pending *ring.Ring

	plan     []plannedOp
	inflight map[string]*inflight
	// readFloor is this client's per-key monotonic-read watermark: the highest
	// version it has ever observed for the key. Answers below it are rejected
	// (the client re-reads), so a single stale replica can never move a read
	// backwards after a newer value was observed.
	readFloor map[string]int
	maxRetry  int
}

func newClient(cfg ClientCfg, w *world) *client {
	c := &client{
		cfg: cfg, aid: clientID(cfg.ID), eng: w.eng, w: w,
		current:   w.rings[0],
		inflight:  map[string]*inflight{},
		readFloor: map[string]int{},
		maxRetry:  w.maxClientAttempts(),
	}
	// Deterministic workload plan, drawn from a per-client RNG derived from the
	// master seed so network scheduling can never perturb the workload.
	ops := cfg.Writes
	if cfg.ReadEvery > 0 {
		// Roughly one read per ReadEvery writes; total slots ~ writes*(1+1/ReadEvery).
	}
	total := ops
	if cfg.ReadEvery > 0 {
		total = ops + ops/cfg.ReadEvery
	}
	rng := w.clientRNG(cfg.ID)
	plan := make([]plannedOp, 0, total)
	slot := 0
	writesPlanned := 0
	readsPlanned := 0
	for writesPlanned < ops {
		isRead := cfg.ReadEvery > 0 && (slot+1)%(cfg.ReadEvery+1) == 0 && readsPlanned < ops/cfg.ReadEvery
		at := int64(cfg.StartTick)
		if cfg.EndTick > cfg.StartTick && total > 1 {
			at += int64(rng.Intn(cfg.EndTick - cfg.StartTick + 1))
		}
		plan = append(plan, plannedOp{at: at, write: !isRead, idx: slot})
		if isRead {
			readsPlanned++
		} else {
			writesPlanned++
		}
		slot++
	}
	// Deterministic order by time (stable preserves generation on ties).
	sort.SliceStable(plan, func(i, j int) bool { return plan[i].at < plan[j].at })
	c.plan = plan
	for _, p := range plan {
		pp := p
		w.eng.after(pp.at, c, "issue", pp)
	}
	return c
}

func (c *client) id() string { return c.aid }

func (c *client) valueFor(planIdx int) string {
	return fmt.Sprintf("v:%s:%d", c.cfg.ID, planIdx)
}

func (c *client) receive(m *Msg) {
	switch b := m.Body.(type) {
	case *TopologyAnnounce:
		c.eng.send(&Msg{Type: "TopologyAck", From: c.aid, To: m.From, Seq: m.Seq, Body: &TopologyAck{Version: b.Version}})
		r, err := ring.New(b.Version, c.w.nodeSpecs(b.Nodes), c.w.cfg.VNodes)
		if err != nil {
			panic(err)
		}
		if b.Pending {
			c.pending = r
		} else if c.current.Version < b.Version {
			c.current = r
			c.pending = nil
		}
	case *CommitAnnounce:
		c.eng.send(&Msg{Type: "CommitAck", From: c.aid, To: m.From, Seq: m.Seq, Body: &CommitAck{Version: b.Version}})
		if c.pending != nil && c.pending.Version == b.Version {
			c.current = c.pending
			c.pending = nil
		}
	case *WriteAck:
		c.onWriteAck(b)
	case *ReadResp:
		c.onReadResp(m.From, b)
	}
}

func (c *client) timer(kind string, data any) {
	switch kind {
	case "issue":
		p := data.(plannedOp)
		if p.write {
			c.startWrite(p.idx)
		} else {
			c.startRead(p.idx)
		}
	case "wretry":
		c.retryWrite(data.(string))
	case "rretry":
		c.retryRead(data.(string))
	}
}

// ---- writes -------------------------------------------------------------------

func (c *client) startWrite(planIdx int) {
	key := c.w.keyFor(c.cfg.ID, planIdx)
	reqID := fmt.Sprintf("w:%s:%d", c.cfg.ID, planIdx)
	val := c.valueFor(planIdx)
	c.w.stats.WritesIssued++
	inf := &inflight{key: key, reqID: reqID, value: val, write: true, attempts: 1,
		issueTick: c.eng.tick(), to: nodeID(c.current.Owner(key))}
	c.inflight[reqID] = inf
	c.sendWrite(inf)
	inf.tid = c.eng.after(int64(c.w.cfg.RetryTicks), c, "wretry", reqID)
}

func (c *client) sendWrite(inf *inflight) {
	c.eng.send(&Msg{Type: "WriteReq", From: c.aid, To: inf.to, Seq: c.w.newSeq(),
		Body: &WriteReq{Key: inf.key, Value: inf.value, ReqID: inf.reqID}})
}

func (c *client) onWriteAck(a *WriteAck) {
	inf := c.inflight[a.ReqID]
	if inf == nil {
		return // already completed, duplicated ack
	}
	if a.Redirect != "" {
		// Migration bounce: follow to the new owner, same request id.
		c.eng.cancelTimer(inf.tid)
		inf.to = a.Redirect
		inf.attempts++
		c.sendWrite(inf)
		inf.tid = c.eng.after(int64(c.w.cfg.RetryTicks), c, "wretry", a.ReqID)
		return
	}
	c.eng.cancelTimer(inf.tid)
	delete(c.inflight, a.ReqID)
	c.w.stats.WritesConfirmed++
	c.w.ledger.noteConfirm(a.ReqID, ConfirmedWrite{
		Key: a.Key, Ver: a.Ver, Value: a.Value, ConfirmedTick: c.eng.tick(),
	})
}

func (c *client) retryWrite(reqID string) {
	inf, ok := c.inflight[reqID]
	if !ok {
		return
	}
	inf.attempts++
	if inf.attempts > c.maxRetry {
		delete(c.inflight, reqID)
		c.w.stats.WritesFailed++
		return
	}
	// Re-target on the freshest view in case topology moved while we waited.
	if c.pending != nil {
		// Prefer the new owner only once it is known to serve; otherwise keep
		// hitting the current owner and follow its redirects.
	}
	c.sendWrite(inf)
	inf.tid = c.eng.after(int64(c.w.cfg.RetryTicks), c, "wretry", reqID)
}

// ---- reads --------------------------------------------------------------------

func (c *client) startRead(planIdx int) {
	key := c.w.keyFor(c.cfg.ID, planIdx)
	rid := fmt.Sprintf("r:%s:%d", c.cfg.ID, planIdx)
	c.w.stats.ReadsIssued++
	inf := &inflight{key: key, reqID: rid, write: false, attempts: 1, issueTick: c.eng.tick(),
		readSeen: map[string]bool{}}
	c.inflight[rid] = inf
	c.sendRead(inf)
	inf.tid = c.eng.after(int64(c.w.cfg.RetryTicks), c, "rretry", rid)
}

func (c *client) readTargets(key string) []string {
	t := nodeID(c.current.Owner(key))
	if c.pending != nil {
		if n2 := nodeID(c.pending.Owner(key)); n2 != t {
			return []string{t, n2} // dual read: old + new owner
		}
	}
	return []string{t}
}

func (c *client) sendRead(inf *inflight) {
	targets := c.readTargets(inf.key)
	inf.readWant = len(targets)
	for _, t := range targets {
		c.eng.send(&Msg{Type: "ReadReq", From: c.aid, To: t, Seq: c.w.newSeq(),
			Body: &ReadReq{RID: inf.reqID, Key: inf.key}})
	}
}

func (c *client) onReadResp(from string, r *ReadResp) {
	inf := c.inflight[r.RID]
	if inf == nil || inf.readSeen[from] {
		return // duplicate answer from the same node: ignore
	}
	inf.readSeen[from] = true
	// Reject answers below this client's monotonic-read watermark.
	if r.Found && r.Ver < c.readFloor[r.Key] {
		inf.readStale++
		return
	}
	if r.Found && (!inf.readFound || r.Ver > inf.readVer) {
		inf.readFound = true
		inf.readVer = r.Ver
		inf.readVal = r.Value
	}
}

func (c *client) retryRead(rid string) {
	inf, ok := c.inflight[rid]
	if !ok {
		return
	}
	// The single-version arbiter:
	//  - if at least one target returned the record, take the greatest version;
	//  - accept not-found only when every queried owner answered not-found;
	//  - reject the result if it is below the client's current monotonic-read
	//    floor, even when that answer looked fresh when it arrived: another
	//    overlapping read on the same client may have raised the floor while
	//    this read was still in flight.
	allAnswered := len(inf.readSeen) >= inf.readWant
	floor := c.readFloor[inf.key]
	staleByFloor := inf.readFound && inf.readVer < floor
	// Not-found is only valid if this client has never observed the key: a
	// later empty answer after a prior value would also move the read back.
	acceptEmpty := allAnswered && !inf.readFound && inf.readStale == 0 && floor == 0
	if (inf.readFound && !staleByFloor) || acceptEmpty {
		c.eng.cancelTimer(inf.tid)
		delete(c.inflight, rid)
		if inf.readFound && inf.readVer > c.readFloor[inf.key] {
			c.readFloor[inf.key] = inf.readVer
		}
		c.w.stats.ReadsSucceeded++
		c.w.ledger.noteRead(ReadObservation{
			Client: c.cfg.ID,
			Key:    inf.key, IssueTick: inf.issueTick, RespTick: c.eng.tick(),
			Ver: inf.readVer, Found: inf.readFound, Value: inf.readVal, Success: true,
		})
		return
	}
	inf.attempts++
	if inf.attempts > c.maxRetry {
		delete(c.inflight, rid)
		c.w.stats.ReadsFailed++
		return
	}
	// Re-issue; clear this attempt's answers (the monotonic floor survives in
	// c.readFloor), so lost or stale answers in one round cannot be mistaken
	// for a final result.
	inf.readSeen = map[string]bool{}
	inf.readStale = 0
	inf.readFound = false
	inf.readVer = 0
	inf.readVal = ""
	c.sendRead(inf)
	inf.tid = c.eng.after(int64(c.w.cfg.RetryTicks), c, "rretry", rid)
}
