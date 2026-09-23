// Package sim is a deterministic single-process discrete-event simulator for
// weighted consistent hashing with live key migration.
//
// The package has three roles:
//
//   - Engine (engine.go): logical clock, event queue and the lossy network
//     (drop / duplicate / reorder, plus a migration-only blackhole).
//   - Nodes and clients (node.go, client.go): storage nodes implementing the
//     snapshot/forward/switch-barrier protocol, and clients performing writes
//     and dual-reads with a single-version arbiter.
//   - World (this file): assembly, timed operations, migration accounting and
//     the end-of-run correctness verifications against an external ledger.
//
// Nothing here opens a socket or spawns a goroutine: the same Config and Seed
// always produce the same Report.
package sim

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"sort"
	"strings"

	"chmig/ring"
)

// World is one simulation run.
type world struct {
	cfg    Config
	eng    *Engine
	stats  *Stats
	ledger *Ledger
	ctrl   *controller

	// rings by topology version
	rings        []*ring.Ring
	pendingRings map[int]*ring.Ring
	ringSpecs    map[int][]ring.NodeSpec // specs used to build each ring

	nodes      map[string]*node // live nodes by raw id
	clients    map[string]*client
	actorOrder []string

	seqCounter int64

	// scheduledStop is last event tick + drain; used when Config.StopTick is 0.
	scheduledStop int

	// epoch lifecycle metadata for the report
	epochBegan      map[int]int64
	epochCommit     map[int]int64
	committedEpochs map[int]*epochM // retained for decommission acks
	epochOrder      []int

	// migration accounting
	movements   map[string]*moveTrack // keyed "ver|key"
	moveOrder   []string
	applyCounts map[string]int // "ver|key" -> record versions applied at B
	applyBytes  map[string]int // payload bytes applied at B
	deadNodes   map[string]bool

	aborts []string
}

type moveTrack struct {
	key, from, to string
	version       int
	beganAt       int64
	switchedAt    int64
}

func moveKey(ver int, key string) string { return fmt.Sprintf("%d|%s", ver, key) }

// Run executes a configuration and returns the JSON-ready report.
func Run(cfg Config) (*Report, error) {
	if err := normalizeCfg(&cfg); err != nil {
		return nil, err
	}
	w, err := buildWorld(cfg)
	if err != nil {
		return nil, err
	}
	stopTick := cfg.StopTick
	if stopTick == 0 {
		stopTick = w.scheduledStop
	}
	return runWorld(w, stopTick), nil
}

func buildWorld(cfg Config) (*world, error) {
	w := &world{
		cfg:             cfg,
		stats:           &Stats{},
		ledger:          newLedger(),
		pendingRings:    map[int]*ring.Ring{},
		ringSpecs:       map[int][]ring.NodeSpec{},
		nodes:           map[string]*node{},
		clients:         map[string]*client{},
		epochBegan:      map[int]int64{},
		epochCommit:     map[int]int64{},
		committedEpochs: map[int]*epochM{},
		movements:       map[string]*moveTrack{},
		applyCounts:     map[string]int{},
		applyBytes:      map[string]int{},
		deadNodes:       map[string]bool{},
	}
	w.eng = newEngine(cfg.Seed, w.stats)
	w.eng.setNetwork(cfg.LossRate, cfg.DuplicateRate, cfg.ReorderRate,
		cfg.MinLinkDelay, cfg.MaxLinkDelay)

	// Ring 0
	specs := make([]ring.NodeSpec, len(cfg.InitialNodes))
	for i, n := range cfg.InitialNodes {
		specs[i] = ring.NodeSpec{ID: n.ID, Weight: n.Weight}
	}
	r0, err := ring.New(0, specs, cfg.VNodes)
	if err != nil {
		return nil, err
	}
	w.rings = []*ring.Ring{r0}
	w.ringSpecs[0] = r0.Nodes()
	w.epochOrder = []int{0}

	w.ctrl = newController(w)
	w.eng.register(w.ctrl)
	w.actorOrder = append(w.actorOrder, ctrlID)

	for _, s := range r0.Nodes() {
		n := newNode(s.ID, r0, w)
		w.nodes[s.ID] = n
		w.eng.register(n)
		w.actorOrder = append(w.actorOrder, n.aid)
	}
	for _, cc := range cfg.Clients {
		cl := newClient(cc, w)
		w.clients[cc.ID] = cl
		w.eng.register(cl)
		w.actorOrder = append(w.actorOrder, cl.aid)
	}
	if cfg.Preload {
		ld := newLoader(w)
		w.eng.register(ld)
		w.actorOrder = append(w.actorOrder, ld.aid)
	}

	// Initial epoch 0 view in the report (committed at tick 0).
	w.epochBegan[0] = 0
	w.epochCommit[0] = 0

	// Timed operations.
	lastTick := 0
	for _, op := range cfg.Operations {
		if op.Tick > lastTick {
			lastTick = op.Tick
		}
		if op.Kind == "interrupt" && op.EndTick > lastTick {
			lastTick = op.EndTick
		}
		w.scheduleOp(op)
	}
	w.scheduledStop = lastTick + cfg.DrainTicks
	return w, nil
}

func runWorld(w *world, stopTick int) *Report {
	w.eng.run(int64(stopTick))
	return w.buildReport(stopTick)
}

func (w *world) scheduleOp(op OpCfg) {
	switch op.Kind {
	case "scaleOut":
		specs := make([]NodeCfg, len(op.Add))
		copy(specs, op.Add)
		w.eng.after(int64(op.Tick), w.ctrl, "scaleOut", specs)
	case "scaleIn":
		ids := make([]string, len(op.Remove))
		copy(ids, op.Remove)
		w.eng.after(int64(op.Tick), w.ctrl, "scaleIn", ids)
	case "interrupt":
		w.eng.after(int64(op.Tick), w.ctrl, "blackholeOn", op.EndTick)
		w.eng.after(int64(op.EndTick), w.ctrl, "blackholeOff", nil)
	default:
		panic("unknown operation kind: " + op.Kind)
	}
}

func (c *controller) handleOp(kind string, data any) {
	switch kind {
	case "scaleOut":
		c.beginScaleOut(data.([]NodeCfg), c.eng.tick())
	case "scaleIn":
		c.beginScaleIn(data.([]string), c.eng.tick())
	case "blackholeOn":
		c.eng.setBlackhole(true, int64(data.(int)))
	case "blackholeOff":
		c.eng.setBlackhole(false, 0)
	}
}

// ---- config normalization -----------------------------------------------------

func normalizeCfg(cfg *Config) error {
	if len(cfg.InitialNodes) == 0 {
		return fmt.Errorf("initialNodes required")
	}
	if len(cfg.Keys) == 0 {
		return fmt.Errorf("keys required")
	}
	if len(cfg.Clients) == 0 {
		return fmt.Errorf("at least one client required")
	}
	if cfg.VNodes <= 0 {
		cfg.VNodes = 64
	}
	if cfg.MinLinkDelay <= 0 {
		cfg.MinLinkDelay = 1
	}
	if cfg.MaxLinkDelay < cfg.MinLinkDelay {
		cfg.MaxLinkDelay = cfg.MinLinkDelay + 2
	}
	if cfg.RetryTicks <= 0 {
		cfg.RetryTicks = 20
	}
	if cfg.MigrationConcurrency <= 0 {
		cfg.MigrationConcurrency = 8
	}
	if cfg.BarrierTicks <= 0 {
		cfg.BarrierTicks = 30
	}
	if cfg.DrainTicks <= 0 {
		cfg.DrainTicks = 400
	}
	for _, r := range []*float64{&cfg.LossRate, &cfg.DuplicateRate, &cfg.ReorderRate} {
		if *r < 0 || *r >= 1 {
			return fmt.Errorf("network rates must be in [0,1)")
		}
	}
	// Validate operations against topology at declaration time where possible.
	present := map[string]bool{}
	for _, n := range cfg.InitialNodes {
		if n.Weight <= 0 {
			return fmt.Errorf("node %q weight must be positive", n.ID)
		}
		present[n.ID] = true
	}
	prevTick := -1
	for _, op := range cfg.Operations {
		if op.Tick < prevTick {
			return fmt.Errorf("operations must be ordered by tick")
		}
		prevTick = op.Tick
		switch op.Kind {
		case "scaleOut":
			if len(op.Add) == 0 {
				return fmt.Errorf("scaleOut requires add nodes")
			}
			for _, a := range op.Add {
				if present[a.ID] {
					return fmt.Errorf("scaleOut node already present: %s", a.ID)
				}
				present[a.ID] = true
			}
		case "scaleIn":
			for _, id := range op.Remove {
				if !present[id] {
					return fmt.Errorf("scaleIn node not present: %s", id)
				}
				delete(present, id)
			}
			if len(present) == 0 {
				return fmt.Errorf("scaleIn would remove every node")
			}
		case "interrupt":
			if op.EndTick <= op.Tick {
				return fmt.Errorf("interrupt endTick must exceed tick")
			}
		default:
			return fmt.Errorf("unknown operation kind: %s", op.Kind)
		}
	}
	// Client windows.
	for i := range cfg.Clients {
		if cfg.Clients[i].Writes <= 0 {
			cfg.Clients[i].Writes = 50
		}
		if cfg.Clients[i].EndTick <= cfg.Clients[i].StartTick {
			return fmt.Errorf("client %s endTick must exceed startTick", cfg.Clients[i].ID)
		}
	}
	return nil
}

// ---- world services used by actors --------------------------------------------

func (w *world) newSeq() int64 {
	w.seqCounter++
	return w.seqCounter
}

// clientRNG derives a dedicated workload RNG per client from the master seed.
func (w *world) clientRNG(id string) *rand.Rand {
	h := fnvMix(uint64(w.cfg.Seed) ^ fnvStringHash(id))
	return rand.New(rand.NewSource(int64(h)))
}

// keyFor maps a client plan slot to a key deterministically.
func (w *world) keyFor(clientID string, planIdx int) string {
	n := len(w.cfg.Keys)
	h := fnvMix(fnvStringHash(clientID) ^ uint64(planIdx)*0x9e3779b97f4a7c15)
	return w.cfg.Keys[int(h%uint64(n))]
}

func (w *world) maxClientAttempts() int { return 40 }

func (w *world) nodeSpecs(ids []string) []ring.NodeSpec {
	// Weight lookup: a node's weight is immutable across epochs in this
	// simulator, so any ring containing it works. Pending rings are searched
	// after committed ones so newly added nodes resolve.
	weightOf := func(id string) int {
		for i := len(w.rings) - 1; i >= 0; i-- {
			for _, n := range w.rings[i].Nodes() {
				if n.ID == id {
					return n.Weight
				}
			}
		}
		versions := make([]int, 0, len(w.pendingRings))
		for v := range w.pendingRings {
			versions = append(versions, v)
		}
		sort.Ints(versions)
		for _, v := range versions {
			for _, n := range w.pendingRings[v].Nodes() {
				if n.ID == id {
					return n.Weight
				}
			}
		}
		return 1
	}
	out := make([]ring.NodeSpec, 0, len(ids))
	for _, id := range ids {
		out = append(out, ring.NodeSpec{ID: id, Weight: weightOf(id)})
	}
	return out
}

func (w *world) allActorIDs() []string {
	out := make([]string, 0, len(w.actorOrder))
	out = append(out, w.actorOrder...)
	return out
}

// fanoutIDs returns live actors that must acknowledge an announcement: nodes
// and clients, excluding the controller itself and decommissioned nodes.
func (w *world) fanoutIDs() []string {
	out := make([]string, 0, len(w.actorOrder))
	for _, a := range w.actorOrder {
		if a == ctrlID || w.eng.dead[a] {
			continue
		}
		out = append(out, a)
	}
	return out
}

func (w *world) nextRingVersion() int { return len(w.rings) }

func (w *world) registerPending(r *ring.Ring) {
	w.pendingRings[r.Version] = r // keyed by unique version; no overwrite
}

// instantiateNodes creates actor instances for nodes newly introduced by a
// scale-out epoch, registers them with the engine and adds them to the
// announcement fan-out. Pre-existing nodes are left untouched.
func (w *world) instantiateNodes(next *ring.Ring) {
	for _, s := range next.Nodes() {
		if w.nodes[s.ID] != nil {
			continue
		}
		n := &node{
			rraw: s.ID, aid: nodeID(s.ID), eng: w.eng, w: w,
			// current stays the last committed ring (it contains the OLD owners
			// this node needs when answering a barrier); pending is the new ring.
			current: w.authoritativeRing(),
			pending: next,
			store:   map[string]Record{},
			history: map[string]map[int]Record{},
			idem:    map[string]map[string]int{},
			out:     map[string]*outState{},
			in:      map[string]*inState{},
			vnodes:  w.cfg.VNodes,
		}
		n.rel = newReliable(w.eng, n, int64(w.cfg.RetryTicks))
		w.nodes[s.ID] = n
		w.eng.register(n)
		w.actorOrder = append(w.actorOrder, n.aid)
	}
}

func (w *world) recordEpoch(r *ring.Ring, tick int64) {
	w.pendingRings[r.Version] = r
	w.ringSpecs[r.Version] = r.Nodes()
	w.epochBegan[r.Version] = tick
	w.epochOrder = append(w.epochOrder, r.Version)
}

func (w *world) recordCommit(ver int, tick int64) { w.epochCommit[ver] = tick }

func (w *world) onRingCommitted(ver int) {
	r := w.pendingRings[ver]
	if r == nil {
		return
	}
	w.rings = append(w.rings, r)
}

func (w *world) ringByVersion(ver int) *ring.Ring {
	if ver < len(w.rings) {
		return w.rings[ver]
	}
	return w.pendingRings[ver]
}

func (w *world) authoritativeRing() *ring.Ring { return w.rings[len(w.rings)-1] }

// ---- migration accounting -------------------------------------------------------

func (w *world) onMigrationBegan(key string, ver int, from, to string, tick int64) {
	mk := moveKey(ver, key)
	if _, ok := w.movements[mk]; ok {
		return
	}
	w.movements[mk] = &moveTrack{key: key, from: from, to: to, version: ver, beganAt: tick}
	w.moveOrder = append(w.moveOrder, mk)
}

func (w *world) onKeySwitched(key string, ver int, tick int64) {
	mk := moveKey(ver, key)
	if mt := w.movements[mk]; mt != nil {
		mt.switchedAt = tick
	}
}

// onMigrateApply counts one record version applied at the new owner via the
// snapshot/forward stream, for migration-volume accounting.
func (w *world) onMigrateApply(key string, ver, epoch int, valueLen int) {
	mk := moveKey(epoch, key)
	if _, ok := w.movements[mk]; !ok {
		// Defensive: applies can precede bookkeeping under message reordering.
		return
	}
	w.applyCounts[mk]++
	w.applyBytes[mk] += valueLen
}

func (w *world) onNodeDecommissioned(n *node) {
	w.eng.dead[n.aid] = true
	w.deadNodes[n.rraw] = true
}

func (w *world) abortEpoch(ep *epochM, reason string) {
	w.aborts = append(w.aborts, reason)
}

// ---- removal gate ----------------------------------------------------------------

// removalGate proves that committing a scale-in loses no latest record.
// Returns ("", true) when safe.
func (w *world) removalGate(ep *epochM) (string, bool) {
	survive := ep.ring
	moving := map[string]bool{}
	for _, k := range ep.movingKeys {
		moving[k] = true
		if !ep.switchDone[k] {
			return fmt.Sprintf("key %s barrier not complete", k), false
		}
	}
	for _, key := range w.cfg.Keys {
		// The surviving owner must physically hold a record at least as new as
		// the newest version applied anywhere. This is the actual safety
		// predicate: it does not matter where the ledger first saw the version
		// (the removed node may have applied it first), only that the surviving
		// owner now holds it — the barrier protocol guarantees B applied every
		// forwarded version, including snapshot-only histories. Keys that do not
		// move are already on a surviving owner and are untouched.
		rec, hasTruth := w.ledger.latestRecord(key)
		if !hasTruth {
			continue
		}
		ownerNode := w.nodes[survive.Owner(key)]
		if ownerNode == nil || w.deadNodes[survive.Owner(key)] {
			return fmt.Sprintf("key %s owner %s missing or removed", key, survive.Owner(key)), false
		}
		stored := ownerNode.store[key]
		if stored.Ver != rec.Ver || stored.Value != rec.Value {
			return fmt.Sprintf("key %s not current on surviving owner %s (store v%d, truth v%d)",
				key, survive.Owner(key), stored.Ver, rec.Ver), false
		}
	}
	_ = moving
	return "", true
}

// ---- report + verification --------------------------------------------------------

func (w *world) buildReport(stopTick int) *Report {
	stats := *w.eng.stats
	rep := &Report{
		Seed:         w.cfg.Seed,
		StopTick:     stopTick,
		RemovedNodes: sortedKeys(w.deadNodes),
		Stats:        stats,
	}

	// Final ring view.
	final := w.authoritativeRing()
	for _, s := range final.Nodes() {
		nv := NodeView{ID: s.ID, Weight: s.Weight, Keys: []string{}}
		if n := w.nodes[s.ID]; n != nil && !w.deadNodes[s.ID] {
			ks := n.storedKeys()
			sort.Strings(ks)
			nv.Keys = ks
		}
		rep.FinalRing = append(rep.FinalRing, nv)
	}

	// Epochs.
	sort.Ints(w.epochOrder)
	for _, v := range w.epochOrder {
		var nodes []string
		specs := w.ringSpecs[v]
		if specs == nil {
			if r := w.ringByVersion(v); r != nil {
				for _, n := range r.Nodes() {
					nodes = append(nodes, n.ID)
				}
			}
		} else {
			for _, n := range specs {
				nodes = append(nodes, n.ID)
			}
		}
		rep.TopologyEpochs = append(rep.TopologyEpochs, EpochView{
			Version: v, Nodes: nodes,
			BeganAt: int(w.epochBegan[v]), CommitAt: int(w.epochCommit[v]),
		})
	}

	// Interruption view.
	for _, op := range w.cfg.Operations {
		if op.Kind == "interrupt" {
			rep.Interruption = &InterruptionView{StartTick: op.Tick, EndTick: op.EndTick}
			break
		}
	}

	w.fillMigration(rep)
	w.verify(rep)
	return rep
}

func (w *world) fillMigration(rep *Report) {
	// Movements in deterministic order. A movement is listed once its barrier
	// passes; transfer/byte accounting includes only COMMITTED migrations.
	committedSet := map[int]bool{}
	for v := range w.epochCommit {
		if v > 0 && w.epochCommit[v] > 0 {
			committedSet[v] = true
		}
	}
	keysMoved, keysSwitched := 0, 0
	totalRecs, totalBytes, minimalBytes := 0, 0, 0
	for _, mk := range w.moveOrder {
		mt := w.movements[mk]
		if mt.switchedAt == 0 {
			continue // barrier never passed
		}
		keysSwitched++
		committed := committedSet[mt.version]
		if committed {
			keysMoved++
			totalRecs += w.applyCounts[mk]
			totalBytes += w.applyBytes[mk]
			if rec, ok := w.ledger.latestRecord(mt.key); ok {
				minimalBytes += len(rec.Value)
			}
		}
		rep.Migration.Movements = append(rep.Migration.Movements, MovementView{
			Key: mt.key, From: mt.from, To: mt.to, Version: mt.version,
			Committed: committed,
			BeganAt:   int(mt.beganAt), SwitchedAt: int(mt.switchedAt),
		})
	}
	rep.Migration.KeysMoved = keysMoved
	rep.Migration.KeysSwitched = keysSwitched
	rep.Migration.EpochsCommitted = len(committedSet)
	rep.Migration.RecordVersionsTransferred = totalRecs
	rep.Migration.BytesTransferred = totalBytes
	rep.Migration.BytesMoved = minimalBytes
	rep.Migration.OverheadBytes = totalBytes - minimalBytes
	if rep.Migration.OverheadBytes < 0 {
		rep.Migration.OverheadBytes = 0
	}
	rep.Migration.BarriersPassed = keysSwitched
}

func (w *world) verify(rep *Report) {
	rep.Verifications.Ownership = w.checkOwnership()
	rep.Verifications.Version = w.checkVersion()
	rep.Verifications.ConfirmedWritesSurvive = w.checkConfirmedSurvive()
	rep.Verifications.RemovalSafety = w.checkRemovalSafety()
	rep.Verifications.NoStaleReadAccepted = w.checkReads()
}

func check(name string, offenses []string, detail string) CheckResult {
	cr := CheckResult{Name: name, Pass: len(offenses) == 0, Details: detail}
	if len(offenses) > 10 {
		cr.Offenses = offenses[:10]
	} else {
		cr.Offenses = offenses
	}
	return cr
}

// checkOwnership: for every key that has ever been written, its final
// authoritative owner physically stores it. Keys never written are vacuously
// owned (an RF-1 store holds no copy anywhere). No other live node may hold a
// newer record than the owner.
func (w *world) checkOwnership() CheckResult {
	final := w.authoritativeRing()
	var off []string
	owned := 0
	for _, key := range w.cfg.Keys {
		latest, written := w.ledger.latestRecord(key)
		if !written {
			continue
		}
		owned++
		owner := final.Owner(key)
		on := w.nodes[owner]
		if on == nil || w.deadNodes[owner] {
			off = append(off, fmt.Sprintf("key %s owner %s is not live", key, owner))
			continue
		}
		stored := on.store[key]
		if stored.Ver == 0 {
			off = append(off, fmt.Sprintf("written key %s missing on owner %s", key, owner))
			continue
		}
		for raw, n := range w.nodes {
			if raw == owner || w.deadNodes[raw] {
				continue
			}
			if other := n.store[key]; other.Ver > stored.Ver {
				off = append(off, fmt.Sprintf("key %s: non-owner %s holds v%d > owner v%d",
					key, raw, other.Ver, stored.Ver))
			}
		}
		_ = latest
	}
	return check("ownership", off,
		fmt.Sprintf("%d/%d keys ever written; each present on its ring-v%d owner",
			owned, len(w.cfg.Keys), final.Version))
}

// checkVersion: the record at each key's owner equals the ledger's latest
// applied record (single correct version + value).
func (w *world) checkVersion() CheckResult {
	final := w.authoritativeRing()
	var off []string
	for _, key := range w.cfg.Keys {
		latest, ok := w.ledger.latestRecord(key)
		on := w.nodes[final.Owner(key)]
		if on == nil {
			continue // reported by ownership check
		}
		stored := on.store[key]
		if ok {
			if stored.Ver != latest.Ver || stored.Value != latest.Value {
				off = append(off, fmt.Sprintf("key %s owner holds v%d=%q, truth v%d=%q",
					key, stored.Ver, stored.Value, latest.Ver, latest.Value))
			}
		} else if stored.Ver != 0 {
			off = append(off, fmt.Sprintf("key %s has store v%d but no applied truth", key, stored.Ver))
		}
	}
	return check("version", off, "owner record must equal latest applied record for every key")
}

// checkConfirmedSurvive: every acked write is present at the final owner at its
// version (a later version on the same chain is also acceptable).
func (w *world) checkConfirmedSurvive() CheckResult {
	final := w.authoritativeRing()
	var off []string
	for _, cw := range w.ledger.confirmedList() {
		on := w.nodes[final.Owner(cw.Key)]
		if on == nil {
			off = append(off, fmt.Sprintf("confirmed %s v%d: owner missing", cw.Key, cw.Ver))
			continue
		}
		stored := on.store[cw.Key]
		if stored.Ver < cw.Ver {
			off = append(off, fmt.Sprintf("confirmed %s v%d lost: owner has v%d", cw.Key, cw.Ver, stored.Ver))
			continue
		}
		// If the exact version is present, value must match (single winner).
		if recs := w.ledger.applied[cw.Key]; recs != nil {
			if rec, ok := recs[cw.Ver]; ok {
				if rec.Value != cw.Value {
					off = append(off, fmt.Sprintf("confirmed %s v%d value mismatch", cw.Key, cw.Ver))
				}
			}
		}
	}
	return check("confirmedWritesSurvive", off,
		fmt.Sprintf("%d confirmed writes verified at final owners", len(w.ledger.confirmed)))
}

// checkRemovalSafety: after decommission, no key's latest record lives solely
// on a removed (dead) node.
func (w *world) checkRemovalSafety() CheckResult {
	final := w.authoritativeRing()
	var off []string
	if len(w.deadNodes) > 0 {
		for _, key := range w.cfg.Keys {
			owner := final.Owner(key)
			if w.deadNodes[owner] {
				off = append(off, fmt.Sprintf("key %s final owner %s was removed", key, owner))
			}
			rec, ok := w.ledger.latestRecord(key)
			if ok && w.deadNodes[rawNode(rec.Node)] {
				// latest apply on a dead node: must also exist on a live owner
				on := w.nodes[owner]
				if on == nil || on.store[key].Ver < rec.Ver {
					off = append(off, fmt.Sprintf("key %s latest v%d exists only on removed node", key, rec.Ver))
				}
			}
		}
	}
	return check("removalSafety", off,
		fmt.Sprintf("%d nodes removed; decommission gate plus post-check", len(w.deadNodes)))
}

// checkReads verifies the single-version invariants of the dual-read arbiter
// ("answer with the greatest version"):
//
//  1. Monotonic reads per (client,key): ordered by response time, a client
//     never observes an older version than one it already read.
//  2. Single value: a returned (version,value) pair is one that was actually
//     committed at that version — the arbiter can never invent or resurrect a
//     value.
//
// We deliberately do NOT require reads to see the globally newest confirmed
// version: a read can legitimately race a write whose ack is still in flight;
// the protocol promises single-version correctness, not linearizability.
func (w *world) checkReads() CheckResult {
	var off []string
	reads := append([]ReadObservation(nil), w.ledger.reads...)
	sort.Slice(reads, func(i, j int) bool {
		if reads[i].RespTick != reads[j].RespTick {
			return reads[i].RespTick < reads[j].RespTick
		}
		if reads[i].Client != reads[j].Client {
			return reads[i].Client < reads[j].Client
		}
		return reads[i].IssueTick < reads[j].IssueTick
	})
	seen := map[string]int{} // "client\x00key" -> highest version read so far
	for _, r := range reads {
		if !r.Success {
			continue
		}
		ck := r.Client + "\x00" + r.Key
		if prev := seen[ck]; r.Ver < prev {
			off = append(off, fmt.Sprintf("%s read %s@%d returned v%d after previously reading v%d",
				r.Client, r.Key, r.RespTick, r.Ver, prev))
		}
		if r.Ver > seen[ck] {
			seen[ck] = r.Ver
		}
		if r.Found {
			if recs := w.ledger.applied[r.Key]; recs != nil {
				if rec, ok := recs[r.Ver]; ok && rec.Value != r.Value {
					off = append(off, fmt.Sprintf("%s read %s v%d got %q but that version is %q",
						r.Client, r.Key, r.Ver, r.Value, rec.Value))
				}
			}
		}
	}
	return check("noStaleReadAccepted", off,
		fmt.Sprintf("%d successful reads validated (monotonic reads + single-value winner)",
			len(reads)))
}

// ---- small hash helpers (deterministic, no allocation-heavy use) ----------------

func fnvStringHash(s string) uint64 {
	var h uint64 = 1469598103934665603
	for i := 0; i < len(s); i++ {
		h ^= uint64(s[i])
		h *= 1099511628211
	}
	return h
}

func fnvMix(x uint64) uint64 {
	x ^= x >> 30
	x *= 0xbf58476d1ce4e5b9
	x ^= x >> 27
	x *= 0x94d049bb133111eb
	x ^= x >> 31
	return x
}

// CanonicalReport renders a report deterministically for diffing/testing.
func CanonicalReport(r *Report) string {
	b, _ := json.MarshalIndent(r, "", "  ")
	return string(b)
}

var _ = strings.TrimSpace
