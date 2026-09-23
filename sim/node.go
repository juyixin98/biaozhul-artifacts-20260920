package sim

import (
	"chmig/ring"
)

func nodeID(raw string) string { return "node:" + raw }
func rawNode(id string) string {
	if len(id) > 5 && id[:5] == "node:" {
		return id[5:]
	}
	return id
}

// reliable retransmits a logical message (fixed Seq) until acknowledged.
type reliable struct {
	eng      *Engine
	self     actor
	table    map[int64]*retrans
	interval int64
}

type retrans struct {
	msg *Msg
	tid int64
}

func newReliable(eng *Engine, self actor, interval int64) *reliable {
	return &reliable{eng: eng, self: self, table: map[int64]*retrans{}, interval: interval}
}

func (r *reliable) send(m *Msg) {
	r.eng.send(m)
	rt := &retrans{msg: m}
	r.table[m.Seq] = rt
	rt.tid = r.eng.after(r.interval, r.self, "retry", m.Seq)
}

func (r *reliable) ack(seq int64) {
	if rt, ok := r.table[seq]; ok {
		r.eng.cancelTimer(rt.tid)
		delete(r.table, seq)
	}
}

// retransmit resends (called from the timer); returns false if forgotten.
func (r *reliable) retransmit(seq int64) bool {
	rt, ok := r.table[seq]
	if !ok {
		return false
	}
	r.eng.send(rt.msg)
	rt.tid = r.eng.after(r.interval, r.self, "retry", seq)
	return true
}

// ---- per-key migration state on the old owner A ------------------------------

// outState tracks A's outgoing migration for one key. Once the barrier is
// acknowledged (done) A is fully FROZEN for the key: it neither applies nor
// acknowledges any further write, it only redirects to B. That freeze lasts
// until the epoch commits (normal path) or forever (interrupted path). A never
// independently "hands over" ownership — the controller's commit is the switch
// barrier — so an interrupted migration leaves A authoritative with every
// confirmed write still on it.
type outState struct {
	key          string
	ringVer      int
	snapVer      int
	nextSeq      int           // per-key stream seq of the next forward (snapVer+1 at start)
	seqToMsg     map[int]int64 // stream seq -> logical Msg.Seq
	barrierSeq   int64         // Msg.Seq of the barrier, 0 until sent
	frozen       bool
	barrierAcked bool
	done         bool
	beganAt      int64
}

// ---- per-key migration state on the new owner B ------------------------------

type inState struct {
	key          string
	ringVer      int
	snapDone     bool
	appliedSeq   int
	idem         map[string]int // reqID -> version
	buffered     map[int]*MigForward
	barrierWait  bool
	barrierUpTo  int
	done         bool
	pendingWrite []pendingClientWrite // client writes frozen at A, applied here after barrier
}

type pendingClientWrite struct {
	from string
	req  *WriteReq
}

// node is a single in-process simulated storage node.
type node struct {
	rraw string
	aid  string
	eng  *Engine
	w    *world
	rel  *reliable

	// ring topology views
	current *ring.Ring
	pending *ring.Ring

	// store: authoritative local record per key the node holds.
	store map[string]Record
	// history: key -> version -> record, retained so an idempotent ack for a
	// retry returns THAT version's value even when the store has advanced.
	history map[string]map[int]Record
	// idem: key -> reqID -> version, for writes this node applied as owner.
	idem map[string]map[string]int

	out map[string]*outState
	in  map[string]*inState

	// beginPending holds StartMigration messages that arrived before the
	// pending-ring announcement for their epoch. They flush in onAnnounce.
	beginPending []*StartMigration

	vnodes int
}

func newNode(raw string, current *ring.Ring, w *world) *node {
	n := &node{
		rraw:    raw,
		aid:     nodeID(raw),
		eng:     w.eng,
		w:       w,
		current: current,
		store:   map[string]Record{},
		history: map[string]map[int]Record{},
		idem:    map[string]map[string]int{},
		out:     map[string]*outState{},
		in:      map[string]*inState{},
		vnodes:  w.cfg.VNodes,
	}
	n.rel = newReliable(w.eng, n, int64(w.cfg.RetryTicks))
	return n
}

func (n *node) id() string { return n.aid }

// applyWrite commits a write to the local store and records ground truth.
// ver must continue the key's version chain.
func (n *node) applyWrite(key, reqID string, ver int, value string) {
	n.store[key] = Record{Ver: ver, Value: value, ReqID: reqID, AtTick: n.eng.tick(), Node: n.aid}
	if n.history[key] == nil {
		n.history[key] = map[int]Record{}
	}
	n.history[key][ver] = n.store[key]
	if n.idem[key] == nil {
		n.idem[key] = map[string]int{}
	}
	n.idem[key][reqID] = ver
	n.w.ledger.noteApply(key, reqID, ver, value, n.aid, n.eng.tick())
}

// recordMigrationValue stores a value B received via the migration stream
// (snapshot/forward) and records it in history, without assigning ownership
// bookkeeping used for client idempotency.
func (n *node) recordMigrationValue(key, reqID string, ver int, value string) {
	n.store[key] = Record{Ver: ver, Value: value, ReqID: reqID, AtTick: n.eng.tick(), Node: n.aid}
	if n.history[key] == nil {
		n.history[key] = map[int]Record{}
	}
	n.history[key][ver] = n.store[key]
}

// replyStoredVersion answers an idempotent retry with the value recorded at
// THAT version (never the current store head, which may be newer).
func (n *node) replyStoredVersion(to string, w *WriteReq, ver int) {
	value := ""
	if hs := n.history[w.Key]; hs != nil {
		if rec, ok := hs[ver]; ok {
			value = rec.Value
		}
	}
	n.replyWrite(to, w, ver, value, "")
}

func (n *node) lastVer(key string) int { return n.store[key].Ver }

func (n *node) receive(m *Msg) {
	switch b := m.Body.(type) {
	case *TopologyAnnounce:
		n.onAnnounce(m, b)
	case *CommitAnnounce:
		n.onCommit(m, b)
	case *Decommission:
		n.onDecommission(m, b)

	case *WriteReq:
		n.onWrite(m, b)
	case *ReadReq:
		n.onRead(m, b)

	case *StartMigration:
		n.onBegin(m, b)
	case *StartBarrier:
		n.onStartBarrier(m, b)
	case *MigSnapshot:
		n.onSnapshot(m, b) // arrives at B
	case *MigForward:
		n.onForward(m, b)
	case *MigAck:
		n.onMigAck(m, b)
	case *MigBarrier:
		n.onBarrier(m, b)
	case *MigBarrierAck:
		n.onBarrierAck(m, b)
	}
}

func (n *node) timer(kind string, data any) {
	if kind == "retry" {
		n.rel.retransmit(data.(int64))
	}
}

// ---- topology ----------------------------------------------------------------

func (n *node) onAnnounce(m *Msg, a *TopologyAnnounce) {
	n.eng.send(&Msg{Type: "TopologyAck", From: n.aid, To: m.From, Seq: m.Seq, Body: &TopologyAck{Version: a.Version}})
	if a.Pending {
		r, err := ring.New(a.Version, n.w.nodeSpecs(a.Nodes), n.vnodes)
		if err != nil {
			panic(err)
		}
		n.pending = r
		// Flush BEGINs that beat the announcement (they had nowhere to resolve
		// the destination owner yet).
		pending := n.beginPending
		n.beginPending = nil
		for _, b := range pending {
			if b.RingVer == a.Version {
				n.onBegin(nil, b)
			}
		}
	}
}

func (n *node) onCommit(m *Msg, c *CommitAnnounce) {
	n.eng.send(&Msg{Type: "CommitAck", From: n.aid, To: m.From, Seq: m.Seq, Body: &CommitAck{Version: c.Version}})
	if n.pending != nil && n.pending.Version == c.Version {
		n.current = n.pending
		n.pending = nil
	}
	// Finalize migration streams for this epoch: B becomes owner, applies the
	// writes it buffered after the barrier (these were never acknowledged) and
	// promotes its dedup table. A drops its outgoing bookkeeping. Stored copies
	// on A are retained until explicit decommission so the removal gate can
	// inspect them.
	for key, st := range n.in {
		if st.ringVer != c.Version {
			continue
		}
		// Drain buffered client writes in arrival order as the newly committed
		// owner. Version chain continues from the value migrated via the stream.
		for _, p := range st.pendingWrite {
			w := p.req
			if ver, ok := st.idem[w.ReqID]; ok {
				n.replyStoredVersion(p.from, w, ver)
				continue
			}
			ver := n.lastVer(w.Key) + 1
			n.applyWrite(w.Key, w.ReqID, ver, w.Value)
			st.idem[w.ReqID] = ver
			n.replyWrite(p.from, w, ver, w.Value, "")
		}
		if n.idem[key] == nil {
			n.idem[key] = map[string]int{}
		}
		for req, ver := range st.idem {
			n.idem[key][req] = ver
		}
		delete(n.in, key)
	}
	for key, st := range n.out {
		if st.ringVer == c.Version {
			delete(n.out, key)
		}
	}
}

func (n *node) onDecommission(m *Msg, d *Decommission) {
	n.eng.send(&Msg{Type: "DecommissionAck", From: n.aid, To: m.From, Seq: m.Seq, Body: &DecommissionAck{Version: d.Version}})
	n.w.onNodeDecommissioned(n)
}

// ---- client writes ------------------------------------------------------------

func (n *node) onWrite(m *Msg, w *WriteReq) {
	key := w.Key

	// Migration in progress as the NEW owner B takes precedence over the
	// current-ring owner test below: B's current ring is still the old one
	// until commit, so without this it would bounce A's redirected writes
	// straight back to A forever.
	if is := n.in[key]; is != nil {
		if ver, ok := is.idem[w.ReqID]; ok {
			n.replyStoredVersion(m.From, w, ver)
			return
		}
		// Until the epoch COMMITS, B never applies a client write directly:
		// before the barrier it buffers and the stream orders it; after the
		// barrier it still buffers because commit (the switch barrier) has not
		// happened. An interrupted migration therefore leaves these writes
		// unacknowledged rather than confirmed-only on a node that may not
		// become owner. They are applied and acked together in onCommit.
		if !is.pendingHas(w.ReqID) {
			is.pendingWrite = append(is.pendingWrite, pendingClientWrite{from: m.From, req: w})
		}
		return
	}

	owner := n.current.Owner(key)
	// Post-commit: a node that is not the owner redirects to the owner.
	if owner != n.rraw {
		n.redirectWrite(m, w, owner)
		return
	}
	// Already applied (duplicated delivery / client retry / post-switch retry)?
	if ver, ok := n.knownReq(key, w.ReqID); ok {
		n.replyStoredVersion(m.From, w, ver)
		return
	}
	// Migration handoff in progress as old owner: apply + forward until frozen,
	// then bounce writes to the new owner (they apply once its barrier closes).
	if os := n.out[key]; os != nil {
		if os.frozen {
			n.redirectWrite(m, w, n.pending.Owner(key))
			return
		}
		ver := n.lastVer(key) + 1
		n.applyWrite(key, w.ReqID, ver, w.Value)
		seq := os.nextSeq
		os.nextSeq++
		fwd := &MigForward{Key: key, RingVer: os.ringVer, SeqNo: seq, ReqID: w.ReqID, Ver: ver, Value: w.Value}
		msg := &Msg{Type: "MigForward", From: n.aid, To: nodeID(n.pending.Owner(key)),
			Seq: n.w.newSeq(), Migration: true, Body: fwd}
		n.rel.send(msg)
		os.seqToMsg[seq] = msg.Seq
		n.replyWrite(m.From, w, ver, w.Value, "")
		return
	}
	// Stable owner path.
	ver := n.lastVer(key) + 1
	n.applyWrite(key, w.ReqID, ver, w.Value)
	n.replyWrite(m.From, w, ver, w.Value, "")
}

func (is *inState) pendingHas(req string) bool {
	for _, p := range is.pendingWrite {
		if p.req.ReqID == req {
			return true
		}
	}
	return false
}

func (n *node) knownReq(key, req string) (int, bool) {
	if v, ok := n.idem[key][req]; ok {
		return v, true
	}
	if is := n.in[key]; is != nil {
		if v, ok := is.idem[req]; ok {
			return v, true
		}
	}
	return 0, false
}

func (n *node) redirectWrite(m *Msg, w *WriteReq, ownerRaw string) {
	n.eng.send(&Msg{Type: "WriteAck", From: n.aid, To: m.From, Seq: n.w.newSeq(),
		Body: &WriteAck{Key: w.Key, ReqID: w.ReqID, Redirect: nodeID(ownerRaw)}})
}

func (n *node) replyWrite(to string, w *WriteReq, ver int, value, redirect string) {
	n.eng.send(&Msg{Type: "WriteAck", From: n.aid, To: to, Seq: n.w.newSeq(),
		Body: &WriteAck{Key: w.Key, ReqID: w.ReqID, Ver: ver, Value: value, Redirect: redirect}})
}

// ---- reads --------------------------------------------------------------------

func (n *node) onRead(m *Msg, r *ReadReq) {
	rec, ok := n.store[r.Key]
	resp := &ReadResp{RID: r.RID, Key: r.Key}
	if ok {
		resp.Found = true
		resp.Ver = rec.Ver
		resp.Value = rec.Value
	}
	n.eng.send(&Msg{Type: "ReadResp", From: n.aid, To: m.From, Seq: n.w.newSeq(), Body: resp})
}

// ---- migration: A side ---------------------------------------------------------

func (n *node) onBegin(m *Msg, b *StartMigration) {
	if n.current.Version >= b.RingVer {
		return
	}
	if _, exists := n.out[b.Key]; exists {
		return // idempotent under controller retransmission
	}
	// The pending-ring announcement has not arrived yet; park the BEGIN. It
	// will be replayed from onAnnounce once n.pending exists.
	if n.pending == nil || n.pending.Version != b.RingVer {
		n.beginPending = append(n.beginPending, b)
		return
	}
	cur := n.store[b.Key]
	os := &outState{
		key: b.Key, ringVer: b.RingVer, snapVer: cur.Ver, nextSeq: cur.Ver + 1,
		seqToMsg: map[int]int64{}, beganAt: n.eng.tick(),
	}
	n.out[b.Key] = os
	dstRaw := n.pending.Owner(b.Key)
	n.w.onMigrationBegan(b.Key, b.RingVer, n.rraw, dstRaw, os.beganAt)
	// Snapshot: current record plus A's per-key idempotency table.
	snap := &MigSnapshot{
		Key: b.Key, RingVer: b.RingVer, Ver: cur.Ver, Value: cur.Value,
		NextVer: cur.Ver + 1, Idempotents: map[string]int{},
	}
	for req, ver := range n.idem[b.Key] {
		snap.Idempotents[req] = ver
	}
	msg := &Msg{Type: "MigSnapshot", From: n.aid, To: nodeID(dstRaw),
		Seq: n.w.newSeq(), Migration: true, Body: snap}
	n.rel.send(msg)
	os.seqToMsg[cur.Ver] = msg.Seq
}

func (n *node) onStartBarrier(m *Msg, b *StartBarrier) {
	if n.current.Version >= b.RingVer {
		return
	}
	os := n.out[b.Key]
	if os == nil {
		return
	}
	if os.barrierSeq != 0 {
		// Re-drive after a lost BarrierDone: the barrier itself already passed.
		// Re-announce completion so the controller can still commit.
		if os.done {
			n.eng.send(&Msg{Type: "BarrierDone", From: n.aid, To: ctrlID, Seq: n.w.newSeq(),
				Body: &BarrierDone{Key: b.Key, RingVer: os.ringVer}})
		}
		return
	}
	os.frozen = true
	bar := &MigBarrier{Key: b.Key, RingVer: os.ringVer, UpTo: os.nextSeq - 1}
	msg := &Msg{Type: "MigBarrier", From: n.aid, To: nodeID(n.pending.Owner(b.Key)),
		Seq: n.w.newSeq(), Migration: true, Body: bar}
	os.barrierSeq = msg.Seq
	n.rel.send(msg)
}

func (n *node) onMigAck(m *Msg, a *MigAck) {
	os := n.out[a.Key]
	if os == nil {
		return
	}
	if seq, ok := os.seqToMsg[a.SeqNo]; ok {
		n.rel.ack(seq)
		delete(os.seqToMsg, a.SeqNo)
	}
}

func (n *node) onBarrierAck(m *Msg, a *MigBarrierAck) {
	os := n.out[a.Key]
	if os == nil || os.done {
		return
	}
	n.rel.ack(os.barrierSeq)
	os.barrierAcked = true
	os.done = true
	// A stays frozen (redirect-only) until the epoch commits. B is caught up
	// and will buffer redirected writes; neither side confirms them until
	// commit, which is what keeps an interrupted migration from stranding a
	// confirmed write on a node about to leave.
	n.w.onKeySwitched(a.Key, os.ringVer, n.eng.tick())
	// Report to the controller; the controller re-drives the barrier if lost.
	n.eng.send(&Msg{Type: "BarrierDone", From: n.aid, To: ctrlID, Seq: n.w.newSeq(),
		Body: &BarrierDone{Key: a.Key, RingVer: os.ringVer}})
}

// ---- migration: B side ----------------------------------------------------------

// pendingFor returns the ring for an in-flight migration epoch. The node's
// own n.pending is authoritative when it matches; otherwise consult the world
// (new nodes may still be processing an earlier epoch announcement).
func (n *node) pendingFor(ver int) *ring.Ring {
	if n.pending != nil && n.pending.Version == ver {
		return n.pending
	}
	return n.w.ringByVersion(ver)
}

func (n *node) onSnapshot(m *Msg, s *MigSnapshot) {
	// A delayed duplicate snapshot for an epoch already committed must not
	// recreate migration state or regress the committed store.
	if n.current.Version >= s.RingVer {
		return
	}
	pending := n.pendingFor(s.RingVer)
	is := n.in[s.Key]
	if is == nil {
		is = &inState{key: s.Key, ringVer: s.RingVer, idem: map[string]int{}, buffered: map[int]*MigForward{}}
		n.in[s.Key] = is
	}
	n.eng.send(&Msg{Type: "MigAck", From: n.aid, To: m.From, Seq: m.Seq, Migration: true,
		Body: &MigAck{Key: s.Key, RingVer: s.RingVer, SeqNo: s.Ver}})
	if is.snapDone {
		return
	}
	is.snapDone = true
	is.appliedSeq = s.Ver
	if s.Ver > 0 {
		n.recordMigrationValue(s.Key, "snap", s.Ver, s.Value)
		n.w.onMigrateApply(s.Key, s.Ver, s.RingVer, len(s.Value))
	}
	for req, ver := range s.Idempotents {
		is.idem[req] = ver
	}
	n.tryBuffered(is)
	_ = pending
}

func (n *node) onForward(m *Msg, f *MigForward) {
	if n.current.Version >= f.RingVer {
		return
	}
	is := n.in[f.Key]
	if is == nil {
		// Snapshot has not arrived yet. Do not ack (that would skip data): A's
		// retransmission applies this forward once the snapshot lands.
		return
	}
	// Ack receipt immediately: stream reliability is enforced by the barrier
	// (appliedSeq), this ack only quiets retransmission of held data.
	n.eng.send(&Msg{Type: "MigAck", From: n.aid, To: m.From, Seq: m.Seq, Migration: true,
		Body: &MigAck{Key: f.Key, RingVer: f.RingVer, SeqNo: f.SeqNo}})
	if f.SeqNo <= is.appliedSeq {
		return
	}
	is.buffered[f.SeqNo] = f
	n.tryBuffered(is)
}

// tryBuffered applies in-order forwards, then completes the barrier / drains
// redirected writes once everything up to barrierUpTo is contiguous.
func (n *node) tryBuffered(is *inState) {
	for {
		f := is.buffered[is.appliedSeq+1]
		if f == nil {
			break
		}
		if _, dup := is.idem[f.ReqID]; !dup {
			n.recordMigrationValue(is.key, f.ReqID, f.Ver, f.Value)
			is.idem[f.ReqID] = f.Ver
			n.w.ledger.noteApply(is.key, f.ReqID, f.Ver, f.Value, n.aid, n.eng.tick())
			n.w.onMigrateApply(is.key, f.Ver, is.ringVer, len(f.Value))
		}
		delete(is.buffered, is.appliedSeq+1)
		is.appliedSeq++
	}
	if is.barrierWait && !is.done && is.appliedSeq >= is.barrierUpTo {
		is.done = true
		// Stream caught up through the freeze point. Note the writes A bounced
		// after freezing are NOT applied/acked here: they wait for the epoch to
		// commit (the switch barrier), so an interrupted, never-committed
		// migration leaves them unconfirmed instead of confirmed on a node that
		// never becomes owner. onCommit drains is.pendingWrite.
		// A keeps retransmitting the barrier until it sees this ack, so a lost
		// ack self-heals: retransmitted barriers elicit this reply again.
		prev := n.w.ringByVersion(is.ringVer - 1)
		n.eng.send(&Msg{Type: "MigBarrierAck", From: n.aid, To: nodeID(prev.Owner(is.key)),
			Seq: n.w.newSeq(), Migration: true, Body: &MigBarrierAck{Key: is.key, RingVer: is.ringVer}})
	}
}

func (n *node) onBarrier(m *Msg, b *MigBarrier) {
	if n.current.Version >= b.RingVer {
		return
	}
	is := n.in[b.Key]
	if is == nil {
		return // snapshot not there yet; A retransmits the barrier
	}
	if is.done {
		// Re-answer a retransmitted barrier so A's retransmission stops.
		n.eng.send(&Msg{Type: "MigBarrierAck", From: n.aid, To: m.From, Seq: n.w.newSeq(), Migration: true,
			Body: &MigBarrierAck{Key: b.Key, RingVer: b.RingVer}})
		return
	}
	is.barrierWait = true
	is.barrierUpTo = b.UpTo
	is.ringVer = b.RingVer
	n.tryBuffered(is)
}

// view helpers
func (n *node) storedKeys() []string {
	out := make([]string, 0, len(n.store))
	for k := range n.store {
		out = append(out, k)
	}
	return out
}
