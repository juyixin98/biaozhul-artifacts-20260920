package sim

import (
	"sort"

	"chmig/ring"
)

const ctrlID = "ctrl"

// epochM tracks the lifecycle of one topology epoch under migration.
type epochM struct {
	version             int
	ring                *ring.Ring
	beganAt             int64
	movingKeys          []string
	switchDone          map[string]bool
	beginSent           map[string]bool
	barrierSent         map[string]bool
	barrierScheduledSet map[string]bool
	// announce/commit reliable-delivery tracking
	annAcked    map[string]bool
	commitAcked map[string]bool
	committed   bool
	removed     []string // nodes removed in this epoch (scale in)
	decomAcked  map[string]bool
}

// controller is the (in-process, simulated) migration orchestrator.
type controller struct {
	aid string
	eng *Engine
	w   *world

	// pending topology epochs in version order; head is the active migration.
	epochs []*epochM
}

func newController(w *world) *controller {
	return &controller{aid: ctrlID, eng: w.eng, w: w}
}

func (c *controller) id() string { return c.aid }

func (c *controller) receive(m *Msg) {
	switch b := m.Body.(type) {
	case *TopologyAck:
		c.onAnnounceAck(m, b)
	case *CommitAck:
		c.onCommitAck(m, b)
	case *BarrierDone:
		c.onBarrierDone(m, b)
	case *DecommissionAck:
		c.onDecomAck(m, b)
	}
}

func (c *controller) timer(kind string, data any) {
	switch kind {
	case "scaleOut", "scaleIn", "blackholeOn", "blackholeOff":
		c.handleOp(kind, data)
	case "resendAnnounce":
		c.resendAnnounce(data.(int))
	case "startBarrier":
		c.sendBarrier(data.(barKey))
	case "beginKey":
		item := data.(migrationTarget)
		c.beginKey(item.ver, item.from, item.key)
	case "resendBegin":
		item := data.(migrationTarget)
		c.beginKey(item.ver, item.from, item.key)
	case "resendBarrier":
		c.resendBarrier(data.(barKey))
	case "commit":
		c.sendCommit(data.(int))
	case "resendCommit":
		c.resendCommit(data.(int))
	case "decom":
		c.resendDecom(data.(int))
	}
}

// ---- timed operations ----------------------------------------------------------

// beginScaleOut builds the next ring with added nodes and starts migration.
func (c *controller) beginScaleOut(add []NodeCfg, tick int64) {
	specs := append(c.currentSpecs(), nodeCfgToSpec(add)...)
	nextVer := c.w.nextRingVersion()
	r, err := ring.New(nextVer, specs, c.w.cfg.VNodes)
	if err != nil {
		panic(err)
	}
	c.startEpoch(r, nil, tick)
}

// beginScaleIn builds the next ring without removed nodes. Removal is gated:
// the epoch can only commit once every key the removed nodes owned has crossed
// a barrier onto a surviving node.
func (c *controller) beginScaleIn(remove []string, tick int64) {
	cur := c.authoritativeRing()
	specs := make([]ring.NodeSpec, 0)
	rm := map[string]bool{}
	for _, id := range remove {
		rm[id] = true
	}
	for _, n := range cur.Nodes() {
		if !rm[n.ID] {
			specs = append(specs, n)
		}
	}
	if len(specs) == 0 {
		panic("scaleIn would remove every node")
	}
	nextVer := c.w.nextRingVersion()
	r, err := ring.New(nextVer, specs, c.w.cfg.VNodes)
	if err != nil {
		panic(err)
	}
	c.startEpoch(r, remove, tick)
}

// startEpoch publishes a pending ring and kicks off per-key migration streams.
func (c *controller) startEpoch(next *ring.Ring, removed []string, tick int64) {
	prev := c.authoritativeRing()
	movements := prev.Diff(next, c.w.cfg.Keys)
	moving := make([]string, 0)
	for _, mv := range movements {
		if mv.Migrates {
			moving = append(moving, mv.Key)
		}
	}
	sort.Strings(moving)

	// Epoch view for the report.
	c.w.recordEpoch(next, tick)

	ep := &epochM{
		version: next.Version, ring: next, beganAt: tick, movingKeys: moving,
		switchDone: map[string]bool{}, beginSent: map[string]bool{},
		barrierSent: map[string]bool{}, barrierScheduledSet: map[string]bool{},
		annAcked: map[string]bool{}, commitAcked: map[string]bool{},
		removed: removed, decomAcked: map[string]bool{},
	}
	c.epochs = append(c.epochs, ep)
	c.w.registerPending(next)
	c.w.instantiateNodes(next)

	// Reliable pending-ring announcement to every node and client.
	c.publishAnnounce(ep, true)

	// Drive migration: only the head epoch proceeds; concurrent epochs queue.
	if c.epochs[0] == ep {
		if len(ep.movingKeys) == 0 {
			// Nothing to migrate: commit on the next tick, after the pending
			// announcements are in flight.
			c.eng.after(1, c, "commit", ep.version)
		} else {
			c.driveEpoch(ep)
		}
	}
}

// driveEpoch opens migration streams for the moving keys (bounded concurrency
// from one old owner) and schedules each key's switch barrier.
func (c *controller) driveEpoch(ep *epochM) {
	prev := c.previousRingFor(ep.version)
	// Deterministic key order: group by (from, key).
	type kv struct{ from, key string }
	list := make([]kv, 0, len(ep.movingKeys))
	for _, k := range ep.movingKeys {
		list = append(list, kv{prev.Owner(k), k})
	}
	sort.Slice(list, func(i, j int) bool {
		if list[i].from != list[j].from {
			return list[i].from < list[j].from
		}
		return list[i].key < list[j].key
	})
	maxC := c.w.cfg.MigrationConcurrency
	for idx, item := range list {
		stagger := int64(0)
		if maxC > 0 {
			// Deterministic staggered start to respect per-source concurrency.
			stagger = int64((idx / maxC) * 2)
		}
		target := migrationTarget{ver: ep.version, from: item.from, key: item.key}
		if stagger == 0 {
			c.beginKey(target.ver, target.from, target.key)
		} else {
			c.eng.after(stagger, c, "beginKey", target)
		}
	}
}

type migrationTarget struct {
	ver  int
	from string
	key  string
}

func (c *controller) beginKey(ver int, from, key string) {
	ep := c.epoch(ver)
	if ep == nil || ep != c.epochs[0] {
		return
	}
	if ep.switchDone[key] {
		return
	}
	if !ep.beginSent[key] {
		ep.beginSent[key] = true
		c.eng.send(&Msg{Type: "StartMigration", From: c.aid, To: nodeID(from),
			Seq: c.w.newSeq(), Body: &StartMigration{Key: key, RingVer: ver}})
	} else {
		// Retransmission (BEGIN or its downstream messages were lost): the node
		// treats BEGIN as idempotent.
		c.eng.send(&Msg{Type: "StartMigration", From: c.aid, To: nodeID(from),
			Seq: c.w.newSeq(), Body: &StartMigration{Key: key, RingVer: ver}})
	}
	// Re-drive BEGIN until the barrier completes; the barrier path itself has
	// its own retransmission once started.
	if !ep.switchDone[key] {
		c.eng.after(int64(c.w.cfg.RetryTicks), c, "resendBegin", migrationTarget{ver, from, key})
	}
	// Barrier schedule: deterministic window after the first BEGIN.
	if !ep.barrierScheduled(key) {
		ep.barrierScheduledSet[key] = true
		c.eng.after(int64(c.w.cfg.BarrierTicks), c, "startBarrier", barKey{ver, key})
	}
}

type barKey struct {
	ver int
	key string
}

func (ep *epochM) barrierScheduled(key string) bool { return ep.barrierScheduledSet[key] }

func (c *controller) sendBarrier(bk barKey) {
	key := bk.key
	ep := c.epoch(bk.ver)
	if ep == nil || ep != c.epochs[0] {
		return
	}
	if !ep.barrierSent[key] {
		ep.barrierSent[key] = true
		prev := c.previousRingFor(ep.version)
		c.eng.send(&Msg{Type: "StartBarrier", From: c.aid, To: nodeID(prev.Owner(key)),
			Seq: c.w.newSeq(), Body: &StartBarrier{Key: key, RingVer: ep.version}})
	}
	// Periodically re-drive until the node reports BarrierDone (interruptions
	// may blackhole the command or the node's completion report).
	if !ep.switchDone[key] {
		c.eng.after(int64(c.w.cfg.RetryTicks), c, "resendBarrier", bk)
	}
}

func (c *controller) resendBarrier(bk barKey) {
	key := bk.key
	ep := c.epoch(bk.ver)
	if ep == nil || ep != c.epochs[0] || ep.switchDone[key] {
		return
	}
	prev := c.previousRingFor(ep.version)
	c.eng.send(&Msg{Type: "StartBarrier", From: c.aid, To: nodeID(prev.Owner(key)),
		Seq: c.w.newSeq(), Body: &StartBarrier{Key: key, RingVer: ep.version}})
	c.eng.after(int64(c.w.cfg.RetryTicks), c, "resendBarrier", bk)
}

func (c *controller) onBarrierDone(m *Msg, b *BarrierDone) {
	var ep *epochM
	for _, e := range c.epochs {
		if e.version == b.RingVer {
			ep = e
			break
		}
	}
	if ep == nil || ep.switchDone[b.Key] {
		return
	}
	ep.switchDone[b.Key] = true
	if len(ep.switchDone) == len(ep.movingKeys) {
		c.eng.after(1, c, "commit", ep.version)
	}
}

// ---- commit + decommission gate -------------------------------------------------

func (c *controller) commitEpoch(ver int) {
	ep := c.epoch(ver)
	if ep == nil || ep.committed || len(ep.switchDone) != len(ep.movingKeys) {
		return
	}
	// Removal safety gate: before committing a scale-in, prove no key whose
	// latest record sits only on a removed node exists. Every key must be
	// fully present on a node that survives the new ring.
	if len(ep.removed) > 0 {
		if offense, ok := c.w.removalGate(ep); !ok {
			// Gate failed: this must never happen given the barrier protocol;
			// record and abort the epoch rather than delete data.
			c.w.abortEpoch(ep, "removal safety gate failed: "+offense)
			return
		}
	}
	ep.committed = true
	c.publishCommit(ep)
}

func (c *controller) publishCommit(ep *epochM) {
	for _, actor := range c.w.fanoutIDs() {
		c.eng.send(&Msg{Type: "CommitAnnounce", From: c.aid, To: actor,
			Seq: c.w.newSeq(), Body: &CommitAnnounce{Version: ep.version}})
	}
	c.eng.after(int64(c.w.cfg.RetryTicks), c, "resendCommit", ep.version)
}

func (c *controller) onCommitAck(m *Msg, a *CommitAck) {
	ep := c.epoch(a.Version)
	if ep == nil {
		return
	}
	ep.commitAcked[rawNodeOrClient(m.From)] = true
	c.maybeFinishDecommission(ep)
}

func (c *controller) maybeFinishDecommission(ep *epochM) {
	// Only live actors must ack; removed (dead) nodes are excluded from the
	// quorum so they cannot stall a later commit.
	for _, ac := range c.w.fanoutIDs() {
		if c.eng.dead[ac] {
			continue
		}
		if !ep.commitAcked[rawNodeOrClient(ac)] {
			return
		}
	}
	c.w.onRingCommitted(ep.version)
	c.w.recordCommit(ep.version, c.eng.tick())
	// Retain the committed epoch (decommission acks reference it), then drop it
	// from the active queue and kick the next queued epoch if present.
	c.w.committedEpochs[ep.version] = ep
	c.epochs = c.epochs[1:]
	if len(c.epochs) > 0 {
		c.driveEpoch(c.epochs[0])
	}
	// Scale-in: after the authoritative commit, decommission removed nodes.
	for _, rn := range ep.removed {
		c.eng.send(&Msg{Type: "Decommission", From: c.aid, To: nodeID(rn),
			Seq: c.w.newSeq(), Body: &Decommission{Version: ep.version}})
	}
	if len(ep.removed) > 0 {
		c.eng.after(int64(c.w.cfg.RetryTicks), c, "decom", ep.version)
	}
}

func (c *controller) resendDecom(ver int) {
	ep := c.epochByVersionCommitted(ver)
	if ep == nil {
		return
	}
	if len(ep.decomAcked) == len(ep.removed) {
		return // all removed nodes have shut down; stop the retransmission timer
	}
	for _, rn := range ep.removed {
		if !ep.decomAcked[rn] {
			c.eng.send(&Msg{Type: "Decommission", From: c.aid, To: nodeID(rn),
				Seq: c.w.newSeq(), Body: &Decommission{Version: ver}})
		}
	}
	c.eng.after(int64(c.w.cfg.RetryTicks), c, "decom", ver)
}

func (c *controller) onDecomAck(m *Msg, d *DecommissionAck) {
	ep := c.epochByVersionCommitted(d.Version)
	if ep == nil {
		return
	}
	ep.decomAcked[rawNode(m.From)] = true
	if len(ep.decomAcked) == len(ep.removed) {
		return
	}
}

// ---- reliable pending announcement ----------------------------------------------

func (c *controller) publishAnnounce(ep *epochM, pending bool) {
	nodeIDs := make([]string, 0, len(ep.ring.Nodes()))
	for _, n := range ep.ring.Nodes() {
		nodeIDs = append(nodeIDs, n.ID)
	}
	for _, actor := range c.w.fanoutIDs() {
		c.eng.send(&Msg{Type: "TopologyAnnounce", From: c.aid, To: actor,
			Seq:  c.w.newSeq(),
			Body: &TopologyAnnounce{Version: ep.version, Nodes: nodeIDs, Pending: pending}})
	}
	c.eng.after(int64(c.w.cfg.RetryTicks), c, "resendAnnounce", ep.version)
}

func (c *controller) resendAnnounce(ver int) {
	var ep *epochM
	for _, e := range c.epochs {
		if e.version == ver {
			ep = e
			break
		}
	}
	if ep == nil || ep.committed {
		return
	}
	nodeIDs := make([]string, 0, len(ep.ring.Nodes()))
	for _, n := range ep.ring.Nodes() {
		nodeIDs = append(nodeIDs, n.ID)
	}
	for _, actor := range c.w.fanoutIDs() {
		if !ep.annAcked[rawNodeOrClient(actor)] {
			c.eng.send(&Msg{Type: "TopologyAnnounce", From: c.aid, To: actor,
				Seq:  c.w.newSeq(),
				Body: &TopologyAnnounce{Version: ver, Nodes: nodeIDs, Pending: true}})
		}
	}
	c.eng.after(int64(c.w.cfg.RetryTicks), c, "resendAnnounce", ver)
}

func (c *controller) onAnnounceAck(m *Msg, a *TopologyAck) {
	if ep := c.epoch(a.Version); ep != nil {
		ep.annAcked[rawNodeOrClient(m.From)] = true
	}
}

func (c *controller) sendCommit(ver int) { c.commitEpoch(ver) }
func (c *controller) resendCommit(ver int) {
	ep := c.epoch(ver)
	if ep == nil || !ep.committed {
		return
	}
	for _, actor := range c.w.fanoutIDs() {
		if !ep.commitAcked[rawNodeOrClient(actor)] {
			c.eng.send(&Msg{Type: "CommitAnnounce", From: c.aid, To: actor,
				Seq: c.w.newSeq(), Body: &CommitAnnounce{Version: ver}})
		}
	}
	c.eng.after(int64(c.w.cfg.RetryTicks), c, "resendCommit", ver)
}

// ---- helpers --------------------------------------------------------------------

func (c *controller) epoch(ver int) *epochM {
	for _, e := range c.epochs {
		if e.version == ver {
			return e
		}
	}
	return nil
}

func (c *controller) epochByVersionCommitted(ver int) *epochM {
	return c.w.committedEpochs[ver]
}

// authoritativeRing is the last committed ring (or ring 0 before any commit).
func (c *controller) authoritativeRing() *ring.Ring {
	return c.w.authoritativeRing()
}

func (c *controller) currentSpecs() []ring.NodeSpec {
	return c.authoritativeRing().Nodes()
}

func (c *controller) previousRingFor(ver int) *ring.Ring {
	return c.w.ringByVersion(ver - 1)
}

func rawNodeOrClient(id string) string { return id }

func nodeCfgToSpec(in []NodeCfg) []ring.NodeSpec {
	out := make([]ring.NodeSpec, len(in))
	for i, n := range in {
		out[i] = ring.NodeSpec{ID: n.ID, Weight: n.Weight}
	}
	return out
}
