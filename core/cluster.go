// Package core implements a single-primary shard migration cutover protocol
// simulator: snapshot -> incremental catch-up -> routed epoch cutover.
//
// All state that decides "who may confirm a write" lives behind one mutex in
// Cluster. Nodes only cache their last-known epoch; every append is therefore
// re-adjudicated against the authoritative routing table under the lock, so a
// node that lost leadership cannot confirm a new write after the switch.
package core

import (
	"errors"
	"fmt"
	"sync"
)

// Phase of the migration state machine.
type Phase string

const (
	PhaseNone      Phase = "none"      // no migration started
	PhaseSnapshot  Phase = "snapshot"  // snapshot point taken, installs may run
	PhaseCatchUp   Phase = "catchup"   // snapshot installed, draining new writes
	PhaseSwitching Phase = "switching" // cutover armed, cluster is write-unavailable
	PhaseDone      Phase = "done"      // epoch cut over to node B
)

// Node names.
const (
	NodeA = "A" // primary of epoch 1
	NodeB = "B" // primary of epoch 2
)

// Sentinel errors, mapped to HTTP status codes in the api package.
var (
	ErrStaleEpoch      = errors.New("stale epoch: write addressed to an old routing version")
	ErrNotPrimary      = errors.New("not primary for the requested epoch")
	ErrClusterFrozen   = errors.New("cluster frozen during cutover")
	ErrNodeDown        = errors.New("node unreachable (simulated partition)")
	ErrBadPhase        = errors.New("control message not valid in the current phase")
	ErrUnknownNode     = errors.New("unknown node")
	ErrResponseDropped = errors.New("response dropped after commit (injected fault)")
)

// Write is one committed, seq-ordered record on the shard.
type Write struct {
	Seq     int64  `json:"seq"`
	Epoch   int    `json:"epoch"`   // epoch under which this write was confirmed
	Primary string `json:"primary"` // node that confirmed it
	Key     string `json:"key"`
	Value   string `json:"value"`
}

// CtrlResult is a cached response to an idempotent control command.
type CtrlResult struct {
	Body     map[string]any `json:"body"`
	Replayed bool           `json:"replayed"`
}

type nodeState struct {
	epoch int      // last epoch pushed to this node (0 = never)
	log   []*Write // node-local confirmed writes
}

// Cluster holds the authoritative shard state.
type Cluster struct {
	mu sync.Mutex

	epoch    int    // current routed epoch
	primary  string // node authoritative for c.epoch
	phase    Phase  // migration phase
	snapshot int64  // seq of the snapshot point (0 if none)
	lastSeq  int64  // highest allocated seq
	nodes    map[string]*nodeState

	// control-link health as seen from the controller
	linksUp map[string]bool

	// client command id -> first successful response (exactly-once control)
	ctrlCache map[string]map[string]any

	// when true, an append commits under the lock but the client never gets
	// the response — models a response lost on the network.
	dropAppendResp map[string]bool
}

// NewCluster creates a shard served by A on epoch 1.
func NewCluster() *Cluster {
	c := &Cluster{
		epoch:          1,
		primary:        NodeA,
		phase:          PhaseNone,
		nodes:          map[string]*nodeState{NodeA: {epoch: 1}, NodeB: {epoch: 0}},
		linksUp:        map[string]bool{NodeA: true, NodeB: true},
		ctrlCache:      map[string]map[string]any{},
		dropAppendResp: map[string]bool{},
	}
	return c
}

// Status is the controller's authoritative view of the shard.
type Status struct {
	Epoch       int             `json:"epoch"`
	Primary     string          `json:"primary"`
	Phase       Phase           `json:"phase"`
	SnapshotSeq int64           `json:"snapshot_seq"`
	LastSeq     int64           `json:"last_seq"`
	LinksUp     map[string]bool `json:"links_up"`
	Nodes       []NodeStatus    `json:"nodes"`
}

// NodeStatus is the per-node part of Status.
type NodeStatus struct {
	Name       string `json:"name"`
	NodeEpoch  int    `json:"node_epoch"`
	IsPrimary  bool   `json:"is_primary"`
	WriteCount int    `json:"write_count"`
	LastSeq    int64  `json:"last_seq"`
}

// Snapshot returns the current status under one lock acquisition.
func (c *Cluster) Snapshot() Status {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.snapshotLocked()
}

func (c *Cluster) snapshotLocked() Status {
	st := Status{
		Epoch:       c.epoch,
		Primary:     c.primary,
		Phase:       c.phase,
		SnapshotSeq: c.snapshot,
		LastSeq:     c.lastSeq,
		LinksUp:     map[string]bool{NodeA: c.linksUp[NodeA], NodeB: c.linksUp[NodeB]},
	}
	for _, n := range []string{NodeA, NodeB} {
		ns := c.nodes[n]
		node := NodeStatus{
			Name:       n,
			NodeEpoch:  ns.epoch,
			IsPrimary:  n == c.primary,
			WriteCount: len(ns.log),
		}
		if len(ns.log) > 0 {
			node.LastSeq = ns.log[len(ns.log)-1].Seq
		}
		st.Nodes = append(st.Nodes, node)
	}
	return st
}

// ---- link / fault injection ------------------------------------------------

// SetLink updates simulated controller<->node link health.
func (c *Cluster) SetLink(node string, up bool) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.nodes[node]; !ok {
		return ErrUnknownNode
	}
	c.linksUp[node] = up
	return nil
}

// SetDropAppend arms/disarms "commit succeeds, response is lost" for a node.
func (c *Cluster) SetDropAppend(node string, drop bool) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.nodes[node]; !ok {
		return ErrUnknownNode
	}
	c.dropAppendResp[node] = drop
	return nil
}

// ---- writes ----------------------------------------------------------------

// AppendResult is returned to the client after a confirmed write.
type AppendResult struct {
	Seq          int64  `json:"seq"`
	Epoch        int    `json:"epoch"`
	Primary      string `json:"primary"`
	Key          string `json:"key"`
	Value        string `json:"value"`
	Deduplicated bool   `json:"deduplicated"`
}

// Append validates and linearises one client write.
//
// nodeEpoch is the routing version the client/node is acting on. Rejection
// order: link down -> cluster frozen -> stale epoch -> not primary.
func (c *Cluster) Append(node string, nodeEpoch int, key, value string) (*AppendResult, bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	ns, ok := c.nodes[node]
	if !ok {
		return nil, false, ErrUnknownNode
	}
	if !c.linksUp[node] {
		return nil, false, ErrNodeDown
	}
	if c.phase == PhaseSwitching {
		return nil, false, ErrClusterFrozen
	}
	if nodeEpoch != c.epoch {
		return nil, false, fmt.Errorf("%w: client used epoch %d, current epoch is %d",
			ErrStaleEpoch, nodeEpoch, c.epoch)
	}
	if node != c.primary {
		return nil, false, fmt.Errorf("%w: %s is not primary of epoch %d",
			ErrNotPrimary, node, c.epoch)
	}

	// Idempotency: a key confirmed once is never confirmed a second time,
	// across retries and across the cutover.
	for _, w := range ns.log {
		if w.Key == key {
			return &AppendResult{
				Seq: w.Seq, Epoch: w.Epoch, Primary: w.Primary,
				Key: w.Key, Value: w.Value, Deduplicated: true,
			}, c.dropAppendResp[node], nil
		}
	}

	c.lastSeq++
	w := &Write{Seq: c.lastSeq, Epoch: c.epoch, Primary: node, Key: key, Value: value}
	ns.log = append(ns.log, w)
	// The "lost response" fault fires once: the commit happens, the first
	// response is discarded, then the link behaves normally so the client's
	// retry reaches the dedupe path and succeeds.
	drop := c.dropAppendResp[node]
	c.dropAppendResp[node] = false
	return &AppendResult{
		Seq: w.Seq, Epoch: w.Epoch, Primary: w.Primary, Key: w.Key, Value: w.Value,
	}, drop, nil
}

// NodeLog returns a copy of one node's confirmed writes.
func (c *Cluster) NodeLog(node string) ([]*Write, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	ns, ok := c.nodes[node]
	if !ok {
		return nil, ErrUnknownNode
	}
	out := make([]*Write, len(ns.log))
	copy(out, ns.log)
	return out, nil
}

// LogRange returns writes with seq in (afterSeq, upToSeq]. A negative
// upToSeq means the whole tail. Used to transfer snapshot and incremental
// batches to B.
func (c *Cluster) LogRange(node string, afterSeq, upToSeq int64) ([]*Write, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	ns, ok := c.nodes[node]
	if !ok {
		return nil, ErrUnknownNode
	}
	var out []*Write
	for _, w := range ns.log {
		if w.Seq <= afterSeq {
			continue
		}
		if upToSeq >= 0 && w.Seq > upToSeq {
			break
		}
		out = append(out, w)
	}
	return out, nil
}

// NodeEpoch returns the epoch last pushed to a node.
func (c *Cluster) NodeEpoch(node string) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	ns, ok := c.nodes[node]
	if !ok {
		return 0, ErrUnknownNode
	}
	return ns.epoch, nil
}

// nodeInstall installs transferred writes on B and bumps its log. Keys already
// present are skipped (safe replay of a duplicated snapshot/catchup batch).
func (c *Cluster) nodeInstall(batch []*Write) (int, int64) {
	ns := c.nodes[NodeB]
	have := map[string]bool{}
	for _, w := range ns.log {
		have[w.Key] = true
	}
	installed := 0
	var last int64
	if len(ns.log) > 0 {
		last = ns.log[len(ns.log)-1].Seq
	}
	for _, w := range batch {
		if have[w.Key] {
			continue
		}
		cp := *w
		ns.log = append(ns.log, &cp)
		have[cp.Key] = true
		installed++
		if cp.Seq > last {
			last = cp.Seq
		}
	}
	return installed, last
}

// InstallOnB is the raw transport-side delivery of transferred writes
// (snapshot or incremental batch). It is key-deduplicated so retransmits of a
// duplicated control message never install twice. The authoritative phase
// advancement happens separately in CompleteSnapshot/CatchUp.
func (c *Cluster) InstallOnB(batch []*Write) (int, int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.nodeInstall(batch)
}

// setNodeEpoch pushes an epoch to a node (B becomes primary of the new epoch).
func (c *Cluster) setNodeEpoch(node string, epoch int) {
	c.nodes[node].epoch = epoch
}

// ---- control plane (exactly-once by client command id) ----------------------

// ctrl runs an idempotent control command. Repeated command IDs replay the
// first successful result with Replayed == true.
func (c *Cluster) ctrl(cmdID string, fn func() (map[string]any, error)) (CtrlResult, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if cmdID != "" {
		if cached, ok := c.ctrlCache[cmdID]; ok {
			body := cloneBody(cached)
			body["replayed"] = true
			return CtrlResult{Body: body, Replayed: true}, nil
		}
	}
	body, err := fn()
	if err != nil {
		return CtrlResult{}, err
	}
	if cmdID != "" {
		body["replayed"] = false
		c.ctrlCache[cmdID] = cloneBody(body)
	}
	return CtrlResult{Body: body}, nil
}

func cloneBody(m map[string]any) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

// BeginSnapshot marks the snapshot point. Idempotent phase advancement:
// calling it again in any later phase reports already=true.
func (c *Cluster) BeginSnapshot(cmdID string) (CtrlResult, error) {
	return c.ctrl(cmdID, func() (map[string]any, error) {
		already := false
		if c.phase == PhaseNone {
			c.phase = PhaseSnapshot
			c.snapshot = c.lastSeq
		} else {
			already = true
		}
		return map[string]any{
			"action":       "begin_snapshot",
			"phase":        c.phase,
			"snapshot_seq": c.snapshot,
			"already":      already,
		}, nil
	})
}

// CompleteSnapshot records the snapshot delivery (the batch itself was pushed
// to B over HTTP by the api layer) and advances to catchup.
func (c *Cluster) CompleteSnapshot(cmdID string, batch []*Write, installed int) (CtrlResult, error) {
	return c.ctrl(cmdID, func() (map[string]any, error) {
		if c.phase == PhaseCatchUp || c.phase == PhaseSwitching || c.phase == PhaseDone {
			return map[string]any{
				"action":  "complete_snapshot",
				"phase":   c.phase,
				"already": true,
			}, nil
		}
		if c.phase != PhaseSnapshot {
			return nil, fmt.Errorf("%w: begin_snapshot required before complete_snapshot (phase=%s)", ErrBadPhase, c.phase)
		}
		if !c.linksUp[NodeB] {
			return nil, fmt.Errorf("install snapshot: %w", ErrNodeDown)
		}
		c.nodeInstall(batch)
		bLast := c.lastSeqOnB()
		c.phase = PhaseCatchUp
		return map[string]any{
			"action":     "complete_snapshot",
			"phase":      c.phase,
			"installed":  installed,
			"b_last_seq": bLast,
		}, nil
	})
}

// CatchUp records one incremental delivery and reports lag. Repeatable — the
// client retries until lag == 0.
func (c *Cluster) CatchUp(cmdID string, batch []*Write, installed int) (CtrlResult, error) {
	return c.ctrl(cmdID, func() (map[string]any, error) {
		if c.phase == PhaseSwitching || c.phase == PhaseDone {
			return map[string]any{
				"action":  "catch_up",
				"phase":   c.phase,
				"already": true,
				"lag":     0,
			}, nil
		}
		if c.phase != PhaseCatchUp {
			return nil, fmt.Errorf("%w: catch_up requires phase catchup (phase=%s)", ErrBadPhase, c.phase)
		}
		if !c.linksUp[NodeA] {
			return nil, fmt.Errorf("read tail from A: %w", ErrNodeDown)
		}
		if !c.linksUp[NodeB] {
			return nil, fmt.Errorf("install tail on B: %w", ErrNodeDown)
		}
		c.nodeInstall(batch)
		bLast := c.lastSeqOnB()
		lag := c.lastSeq - bLast
		if lag < 0 {
			lag = 0
		}
		return map[string]any{
			"action":     "catch_up",
			"phase":      c.phase,
			"installed":  installed,
			"a_last_seq": c.lastSeq,
			"b_last_seq": bLast,
			"lag":        lag,
		}, nil
	})
}

func (c *Cluster) lastSeqOnB() int64 {
	if log := c.nodes[NodeB].log; len(log) > 0 {
		return log[len(log)-1].Seq
	}
	return 0
}

// PrepareCutover freezes writes and records the cutover sequence. B must be
// fully caught up (lag 0) so no committed write is stranded on the retiring A.
func (c *Cluster) PrepareCutover(cmdID string) (CtrlResult, error) {
	return c.ctrl(cmdID, func() (map[string]any, error) {
		already := false
		if c.phase == PhaseSwitching || c.phase == PhaseDone {
			already = true
		} else if c.phase != PhaseCatchUp {
			return nil, fmt.Errorf("%w: prepare_cutover requires phase catchup (phase=%s)", ErrBadPhase, c.phase)
		}
		var bLast int64
		if log := c.nodes[NodeB].log; len(log) > 0 {
			bLast = log[len(log)-1].Seq
		}
		if c.lastSeq != bLast {
			return nil, fmt.Errorf("refuse cutover: B lagging (a_last=%d b_last=%d)", c.lastSeq, bLast)
		}
		c.phase = PhaseSwitching
		return map[string]any{
			"action":      "prepare_cutover",
			"phase":       c.phase,
			"cutover_seq": c.lastSeq,
			"already":     already,
		}, nil
	})
}

// CommitCutover publishes the new routing epoch: B becomes the sole primary.
// Requires the controller<->B link so the epoch push is delivered.
func (c *Cluster) CommitCutover(cmdID string) (CtrlResult, error) {
	return c.ctrl(cmdID, func() (map[string]any, error) {
		if c.phase == PhaseDone {
			return map[string]any{
				"action":  "commit_cutover",
				"phase":   c.phase,
				"epoch":   c.epoch,
				"primary": c.primary,
				"already": true,
			}, nil
		}
		if c.phase != PhaseSwitching {
			return nil, fmt.Errorf("%w: commit_cutover requires phase switching (phase=%s)", ErrBadPhase, c.phase)
		}
		if !c.linksUp[NodeB] {
			return nil, fmt.Errorf("push epoch to B: %w", ErrNodeDown)
		}
		c.epoch++
		c.primary = NodeB
		c.setNodeEpoch(NodeB, c.epoch)
		c.phase = PhaseDone
		return map[string]any{
			"action":  "commit_cutover",
			"phase":   c.phase,
			"epoch":   c.epoch,
			"primary": c.primary,
		}, nil
	})
}

// Reset returns the cluster to its initial state (test/demo convenience).
func (c *Cluster) Reset() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.epoch = 1
	c.primary = NodeA
	c.phase = PhaseNone
	c.snapshot = 0
	c.lastSeq = 0
	c.nodes = map[string]*nodeState{NodeA: {epoch: 1}, NodeB: {epoch: 0}}
	c.linksUp = map[string]bool{NodeA: true, NodeB: true}
	c.ctrlCache = map[string]map[string]any{}
	c.dropAppendResp = map[string]bool{}
}

// ---- verification -----------------------------------------------------------

// Check is one named invariant result from Verify.
type Check struct {
	Name   string `json:"name"`
	Pass   bool   `json:"pass"`
	Detail string `json:"detail,omitempty"`
}

// Verify audits the post-cutover invariants:
//
//  1. sequences are gap-free
//  2. every confirmed write exists on the current primary B (no acknowledged
//     write lost)
//  3. every A-side write is epoch 1, every B-side write is epoch 2, and the
//     primary recorded on each write matches the node holding it
//  4. no key is confirmed by two primaries (no dual-master confirmation)
func (c *Cluster) Verify() []Check {
	c.mu.Lock()
	defer c.mu.Unlock()

	var checks []Check

	aLog := c.nodes[NodeA].log
	bLog := c.nodes[NodeB].log

	// 1. gap-free global sequence on A (the only allocator of epoch 1).
	gapOK := true
	gapDetail := ""
	for i, w := range aLog {
		if w.Seq != int64(i+1) {
			gapOK = false
			gapDetail = fmt.Sprintf("A log index %d has seq %d", i, w.Seq)
			break
		}
	}
	checks = append(checks, Check{"sequences_gap_free", gapOK, gapDetail})

	// 2. every acknowledged write (union by seq) exists on B after cutover.
	bSeqs := map[int64]bool{}
	for _, w := range bLog {
		bSeqs[w.Seq] = true
	}
	missing := []int64{}
	if c.phase == PhaseDone {
		for _, w := range aLog {
			if !bSeqs[w.Seq] {
				missing = append(missing, w.Seq)
			}
		}
	}
	lostOK := len(missing) == 0
	lostDetail := ""
	if !lostOK {
		lostDetail = fmt.Sprintf("B is missing seqs %v", missing)
	}
	checks = append(checks, Check{"all_confirmed_writes_present", lostOK, lostDetail})

	// 3a. A holds only epoch-1 writes confirmed by A.
	epochOK := true
	epochDetail := ""
	for _, w := range aLog {
		if w.Epoch != 1 || w.Primary != NodeA {
			epochOK = false
			epochDetail = fmt.Sprintf("A holds seq %d with epoch=%d primary=%s", w.Seq, w.Epoch, w.Primary)
			break
		}
	}
	// 3b. B's post-cutover writes are epoch 2 confirmed by B; transferred
	// writes keep their original epoch 1 provenance.
	if epochOK {
		for _, w := range bLog {
			if w.Primary == NodeB && (w.Epoch != c.epoch || c.primary != NodeB) {
				epochOK = false
				epochDetail = fmt.Sprintf("B holds new write seq %d with epoch=%d", w.Seq, w.Epoch)
				break
			}
			if w.Primary == NodeA && w.Epoch != 1 {
				epochOK = false
				epochDetail = fmt.Sprintf("B holds transferred seq %d with epoch=%d", w.Seq, w.Epoch)
				break
			}
		}
	}
	checks = append(checks, Check{"epoch_provenance", epochOK, epochDetail})

	// 4. no key is confirmed by two different primaries.
	owner := map[string]string{}
	dual := false
	dualDetail := ""
	for _, w := range append(append([]*Write{}, aLog...), bLog...) {
		if prev, ok := owner[w.Key]; ok && prev != w.Primary {
			dual = true
			dualDetail = fmt.Sprintf("key %q confirmed by both %s and %s", w.Key, prev, w.Primary)
			break
		}
		owner[w.Key] = w.Primary
	}
	checks = append(checks, Check{"no_dual_primary_confirmation", !dual, dualDetail})

	return checks
}
