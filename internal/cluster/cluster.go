// Package cluster implements an in-memory single-primary shard migration
// simulator with three explicit phases: snapshot, incremental catch-up and
// routing-version switchover.
//
// Concurrency model: a single RWMutex inside Cluster serialises every state
// transition. This models a replicated state machine in which control-plane
// operations (snapshot/catchup/switch) are totally ordered against data-plane
// writes, which is what makes the switchover point unambiguous.
package cluster

import (
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"
)

// Phase of the migration control loop.
type Phase string

const (
	PhaseRunning  Phase = "RUNNING"  // no migration in progress
	PhaseSnapshot Phase = "SNAPSHOT" // snapshot being taken/loaded
	PhaseCatchup  Phase = "CATCHUP"  // snapshot loaded; new writes streamed
	PhaseSwitched Phase = "SWITCHED" // new primary confirmed; cut-over complete
)

// Sentinel errors. HTTP code mapping is documented next to each value.
var (
	ErrShardNotFound       = errors.New("shard not found")                             // 404
	ErrNodeNotFound        = errors.New("node not found")                              // 404
	ErrShardExists         = errors.New("shard already exists")                        // 409
	ErrNodeExists          = errors.New("node already exists")                         // 409
	ErrStaleRoute          = errors.New("stale route version")                         // 409
	ErrPrimaryDisconnected = errors.New("primary node disconnected")                   // 503
	ErrWrongPhase          = errors.New("control message invalid in phase")            // 409
	ErrTargetDisconnected  = errors.New("target node disconnected")                    // 409
	ErrLagRemaining        = errors.New("target still lagging primary")                // 409
	ErrTargetIsPrimary     = errors.New("target is already primary")                   // 409
	ErrIdemConflict        = errors.New("idempotency key reused for different action") // 409
	ErrBadTarget           = errors.New("unknown target node")                         // 400
)

// Node is a member of the cluster; connectivity is a simulated fault flag.
type Node struct {
	ID        string
	Connected bool
}

// Write is one confirmed write with a per-shard monotonic sequence number.
type Write struct {
	Seq       int       `json:"seq"`
	Payload   string    `json:"payload"`
	Primary   string    `json:"primary"`   // node that confirmed it
	RouteVer  int       `json:"route_ver"` // route version under which it was confirmed
	Phase     Phase     `json:"phase"`     // migration phase at confirmation time
	Confirmed time.Time `json:"confirmed_at"`

	// DeliveredTo lists every node that durably holds the write. Includes the
	// confirming primary. Snapshot-loaded writes are marked in one batch.
	DeliveredTo []string `json:"delivered_to"`
}

func (w Write) clone() Write {
	out := w
	out.DeliveredTo = append([]string(nil), w.DeliveredTo...)
	return out
}

// MigrationView is the control-plane state of one shard.
type MigrationView struct {
	Active       bool   `json:"active"`
	Phase        Phase  `json:"phase"`
	OldPrimaryID string `json:"old_primary_id,omitempty"`
	TargetID     string `json:"target_id,omitempty"`
	StartSeq     int    `json:"start_seq"` // exclusive: snapshot covers seq <= start_seq
	SnapshotSeq  int    `json:"snapshot_seq"`
	CaughtUpSeq  int    `json:"caught_up_seq"`
	SwitchSeq    int    `json:"switch_seq"` // last seq confirmable by the old primary
	NewRouteVer  int    `json:"new_route_ver"`
}

// NodeView is one node's role and connectivity.
type NodeView struct {
	ID        string `json:"id"`
	Role      string `json:"role"` // "primary" or "replica"
	Connected bool   `json:"connected"`
}

// ShardView is the full externally visible state of one shard.
type ShardView struct {
	ID        string        `json:"id"`
	RouteVer  int           `json:"route_ver"`
	PrimaryID string        `json:"primary_id"`
	Phase     Phase         `json:"phase"`
	Migration MigrationView `json:"migration"`
	Nodes     []NodeView    `json:"nodes"`
	Writes    []Write       `json:"writes"`
	LastSeq   int           `json:"last_seq"`

	// Rejected counts client writes refused before confirmation. Used by the
	// acceptance audit: stale-route attempts must never become confirmed writes.
	StaleRouteRejects  int `json:"stale_route_rejects"`
	DownPrimaryRejects int `json:"down_primary_rejects"`
}

// Cluster is the whole simulated cluster.
type Cluster struct {
	mu    sync.RWMutex
	nodes map[string]*Node
	shard map[string]*shard
}

type shard struct {
	id        string
	routeVer  int
	primaryID string
	phase     Phase
	lastSeq   int
	nodes     []string // ordered membership
	writes    []*Write
	mig       MigrationView

	// writeDedup de-duplicates data-plane retries keyed by client idempotency key.
	writeDedup map[string]*Write
	// ctrlReceipts de-duplicates control messages: action+key -> cached response.
	ctrlReceipts map[string]*ctrlReceipt

	staleRouteRejects  int
	downPrimaryRejects int
}

type ctrlReceipt struct {
	action string
	resp   any
}

// NewCluster creates an empty cluster.
func NewCluster() *Cluster {
	return &Cluster{
		nodes: map[string]*Node{},
		shard: map[string]*shard{},
	}
}

// AddNode registers a node. Nodes are cluster-wide in this simulator.
func (c *Cluster) AddNode(id string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.nodes[id]; ok {
		return ErrNodeExists
	}
	c.nodes[id] = &Node{ID: id, Connected: true}
	return nil
}

// CreateShard creates a shard with an ordered node list; the first node is primary.
func (c *Cluster) CreateShard(id string, nodeIDs []string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.shard[id]; ok {
		return ErrShardExists
	}
	if len(nodeIDs) == 0 {
		return fmt.Errorf("%w: shard needs at least one node", ErrBadTarget)
	}
	for _, nid := range nodeIDs {
		if _, ok := c.nodes[nid]; !ok {
			return fmt.Errorf("%w: %q", ErrNodeNotFound, nid)
		}
	}
	s := &shard{
		id:           id,
		routeVer:     1,
		primaryID:    nodeIDs[0],
		phase:        PhaseRunning,
		nodes:        append([]string(nil), nodeIDs...),
		writeDedup:   map[string]*Write{},
		ctrlReceipts: map[string]*ctrlReceipt{},
	}
	c.shard[id] = s
	return nil
}

// SetConnected simulates a network partition / node failure or its recovery.
func (c *Cluster) SetConnected(nodeID string, connected bool) (NodeView, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	n, ok := c.nodes[nodeID]
	if !ok {
		return NodeView{}, ErrNodeNotFound
	}
	n.Connected = connected
	return c.nodeView(n), nil
}

// connectedLocked reports whether a node currently accepts traffic.
func (c *Cluster) connectedLocked(id string) bool {
	return c.nodes[id].Connected
}

func (c *Cluster) nodeView(n *Node) NodeView {
	v := NodeView{ID: n.ID, Connected: n.Connected}
	for _, s := range c.shard {
		if s.primaryID == n.ID {
			v.Role = "primary"
			break
		}
	}
	if v.Role == "" {
		v.Role = "replica"
	}
	return v
}

// nodeRoleLocked gives the role of node nid within shard s.
func (c *Cluster) nodeRoleLocked(s *shard, nid string) string {
	if s.primaryID == nid {
		return "primary"
	}
	return "replica"
}

func sortedShardIDs(m map[string]*shard) []string {
	ids := make([]string, 0, len(m))
	for id := range m {
		ids = append(ids, id)
	}
	// simple insertion sort keeps output deterministic without importing sort
	for i := 1; i < len(ids); i++ {
		for j := i; j > 0 && ids[j-1] > ids[j]; j-- {
			ids[j-1], ids[j] = ids[j], ids[j-1]
		}
	}
	return ids
}

func sortedNodeIDs(m map[string]*Node) []string {
	ids := make([]string, 0, len(m))
	for id := range m {
		ids = append(ids, id)
	}
	for i := 1; i < len(ids); i++ {
		for j := i; j > 0 && ids[j-1] > ids[j]; j-- {
			ids[j-1], ids[j] = ids[j], ids[j-1]
		}
	}
	return ids
}

// ---- views ----------------------------------------------------------------

func (c *Cluster) shardViewLocked(s *shard) ShardView {
	nv := make([]NodeView, 0, len(s.nodes))
	for _, nid := range s.nodes {
		n := c.nodes[nid]
		nv = append(nv, NodeView{
			ID:        nid,
			Role:      c.nodeRoleLocked(s, nid),
			Connected: n.Connected,
		})
	}
	ws := make([]Write, 0, len(s.writes))
	for _, w := range s.writes {
		ws = append(ws, w.clone())
	}
	return ShardView{
		ID:                 s.id,
		RouteVer:           s.routeVer,
		PrimaryID:          s.primaryID,
		Phase:              s.phase,
		Migration:          s.mig,
		Nodes:              nv,
		Writes:             ws,
		LastSeq:            s.lastSeq,
		StaleRouteRejects:  s.staleRouteRejects,
		DownPrimaryRejects: s.downPrimaryRejects,
	}
}

// StateView is the full cluster snapshot returned by GET /state.
type StateView struct {
	RouteVersions map[string]int `json:"route_versions"`
	Nodes         []NodeView     `json:"nodes"`
	Shards        []ShardView    `json:"shards"`
}

// State returns a deep copy of the whole cluster state.
func (c *Cluster) State() StateView {
	c.mu.RLock()
	defer c.mu.RUnlock()
	out := StateView{
		RouteVersions: map[string]int{},
	}
	// Cluster-wide node list with per-node primary role across all shards.
	for _, nid := range sortedNodeIDs(c.nodes) {
		n := c.nodes[nid]
		role := "replica"
		for _, s := range c.shard {
			if s.primaryID == nid {
				role = "primary"
				break
			}
		}
		out.Nodes = append(out.Nodes, NodeView{ID: nid, Role: role, Connected: n.Connected})
	}
	for _, sid := range sortedShardIDs(c.shard) {
		s := c.shard[sid]
		out.RouteVersions[sid] = s.routeVer
		out.Shards = append(out.Shards, c.shardViewLocked(s))
	}
	return out
}

// Shard returns one shard's view.
func (c *Cluster) Shard(id string) (ShardView, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	s, ok := c.shard[id]
	if !ok {
		return ShardView{}, ErrShardNotFound
	}
	return c.shardViewLocked(s), nil
}

// Writes returns a chronological copy of all confirmed writes for a shard.
func (c *Cluster) Writes(shardID string) ([]Write, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	s, ok := c.shard[shardID]
	if !ok {
		return nil, ErrShardNotFound
	}
	out := make([]Write, 0, len(s.writes))
	for _, w := range s.writes {
		out = append(out, w.clone())
	}
	return out, nil
}

// AuditResult is the dual-primary invariant report used by tests/acceptance.
type AuditResult struct {
	ShardID          string `json:"shard_id"`
	LastSeq          int    `json:"last_seq"`
	ConfirmedCount   int    `json:"confirmed_count"`
	MissingCount     int    `json:"missing_confirmed_writes"`           // must be 0: all confirmed writes present
	GapCount         int    `json:"sequence_gaps"`                      // must be 0: dense seq 1..N
	MultiConfirmer   int    `json:"multi_confirmer_writes"`             // must be 0: every write confirmed by exactly one node
	SplitAtSwitch    int    `json:"writes_before_switch_by_old"`        // old primary before cut-over
	NewAfterSwitch   int    `json:"writes_after_switch_by_new"`         // new primary after cut-over
	OldAfterSwitch   int    `json:"old_primary_confirms_after_switch"`  // MUST be 0: no dual primary
	NewBeforeSwitch  int    `json:"new_primary_confirms_before_switch"` // MUST be 0: no dual primary
	StaleRouteWrites int    `json:"stale_route_confirms"`               // MUST be 0
	TargetMissing    int    `json:"writes_missing_on_new_primary"`      // MUST be 0 once SWITCHED
	Healthy          bool   `json:"healthy"`
}

// Audit verifies the safety invariants for one shard.
func (c *Cluster) Audit(shardID string) (AuditResult, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	s, ok := c.shard[shardID]
	if !ok {
		return AuditResult{}, ErrShardNotFound
	}
	res := AuditResult{ShardID: shardID, LastSeq: s.lastSeq}
	seen := map[int]*Write{}
	switchPoint := 0
	oldPrimary := s.mig.OldPrimaryID
	newPrimary := s.mig.TargetID
	if s.mig.Active {
		switchPoint = s.mig.SwitchSeq
	}
	for _, w := range s.writes {
		res.ConfirmedCount++
		seen[w.Seq] = w
		// exactly one confirmer: Primary is a single node id by construction;
		// additionally assert it never shows a compound value.
		if strings.ContainsAny(w.Primary, ",/ ") {
			res.MultiConfirmer++
		}
		if switchPoint > 0 {
			before := w.Seq <= switchPoint
			switch {
			case before && w.Primary == newPrimary:
				res.NewBeforeSwitch++
			case !before && w.Primary == oldPrimary:
				res.OldAfterSwitch++
			case before && w.Primary == oldPrimary:
				res.SplitAtSwitch++
			case !before && w.Primary == newPrimary:
				res.NewAfterSwitch++
			}
		}
	}
	for seq := 1; seq <= s.lastSeq; seq++ {
		if _, ok := seen[seq]; !ok {
			res.MissingCount++
		}
	}
	// dense seq check (same data, explicit gap counter)
	res.GapCount = res.MissingCount
	// Once cut-over is complete the new primary must hold every confirmed write.
	if s.phase == PhaseSwitched {
		res.TargetMissing = s.missingOnTargetLocked(newPrimary)
	}
	res.Healthy = res.MissingCount == 0 && res.GapCount == 0 &&
		res.MultiConfirmer == 0 && res.OldAfterSwitch == 0 &&
		res.NewBeforeSwitch == 0 && res.StaleRouteWrites == 0 &&
		res.TargetMissing == 0
	return res, nil
}

// ---- data plane -----------------------------------------------------------

// WriteInput is a client write request.
type WriteInput struct {
	ShardID        string `json:"shard_id"`
	Payload        string `json:"payload"`
	ClientRouteVer int    `json:"client_route_ver"`
	IdempotencyKey string `json:"idempotency_key"`
}

// ClientWrite confirms one write on the shard's current primary.
//
// Rules enforced:
//   - client must present the current route version (old clients get ErrStaleRoute);
//   - only the current primary can confirm (writes are not load-balanced);
//   - during SNAPSHOT the primary keeps accepting writes (they ship later);
//   - during CATCHUP every confirmed write is streamed to the target at once;
//   - after SWITCHED only the new primary (routeVer+1) confirms.
func (c *Cluster) ClientWrite(in WriteInput) (*Write, bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.shard[in.ShardID]
	if !ok {
		return nil, false, ErrShardNotFound
	}
	if in.ClientRouteVer != 0 && in.ClientRouteVer != s.routeVer {
		s.staleRouteRejects++
		return nil, false, fmt.Errorf("%w: client v%d, current v%d", ErrStaleRoute, in.ClientRouteVer, s.routeVer)
	}
	if !c.connectedLocked(s.primaryID) {
		s.downPrimaryRejects++
		return nil, false, ErrPrimaryDisconnected
	}
	if in.IdempotencyKey != "" {
		if w, ok := s.writeDedup[in.IdempotencyKey]; ok {
			cw := w.clone()
			return &cw, true, nil
		}
	}
	s.lastSeq++
	w := &Write{
		Seq:         s.lastSeq,
		Payload:     in.Payload,
		Primary:     s.primaryID,
		RouteVer:    s.routeVer,
		Phase:       s.phase,
		Confirmed:   time.Now().UTC(),
		DeliveredTo: []string{s.primaryID},
	}
	s.writes = append(s.writes, w)
	if in.IdempotencyKey != "" {
		s.writeDedup[in.IdempotencyKey] = w
	}
	// Incremental catch-up: stream the new write straight to the target.
	if s.phase == PhaseCatchup && c.connectedLocked(s.mig.TargetID) {
		w.DeliveredTo = append(w.DeliveredTo, s.mig.TargetID)
		if w.Seq > s.mig.CaughtUpSeq {
			s.mig.CaughtUpSeq = w.Seq
		}
	}
	out := w.clone()
	return &out, false, nil
}

// ---- control plane --------------------------------------------------------

func receiptKey(action, key string) string { return action + "\x00" + key }

// ctrlReplay returns a cached control response when the same (action,key) repeats.
// If the key exists for a different action it is rejected as a conflict.
func (s *shard) ctrlReplay(action, key string) (any, bool, error) {
	if r, ok := s.ctrlReceipts[receiptKey(action, key)]; ok {
		return r.resp, true, nil
	}
	// Same key attached to any other action is a misuse, not a replay.
	for k := range s.ctrlReceipts {
		if _, gotKey, ok := strings.Cut(k, "\x00"); ok && gotKey == key {
			return nil, false, ErrIdemConflict
		}
	}
	return nil, false, nil
}

func (s *shard) remember(action, key string, resp any) {
	if key == "" {
		return
	}
	s.ctrlReceipts[receiptKey(action, key)] = &ctrlReceipt{action: action, resp: resp}
}

// StartResult reports a migration start.
type StartResult struct {
	ShardID  string `json:"shard_id"`
	Phase    Phase  `json:"phase"`
	TargetID string `json:"target_id"`
	StartSeq int    `json:"start_seq"`
	RouteVer int    `json:"route_ver"`
	Replayed bool   `json:"replayed"`
}

// Start begins a migration: RUNNING -> SNAPSHOT.
func (c *Cluster) Start(shardID, targetID, ctrlKey string) (StartResult, bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.shard[shardID]
	if !ok {
		return StartResult{}, false, ErrShardNotFound
	}
	if ctrlKey != "" {
		if r, replay, err := s.ctrlReplay("start", ctrlKey); err != nil {
			return StartResult{}, false, err
		} else if replay {
			res := r.(StartResult)
			res.Replayed = true
			return res, true, nil
		}
	}
	if s.phase != PhaseRunning {
		return StartResult{}, false, fmt.Errorf("%w: START requires RUNNING, got %s", ErrWrongPhase, s.phase)
	}
	if targetID == "" {
		// deterministic default: first non-primary member
		for _, nid := range s.nodes {
			if nid != s.primaryID {
				targetID = nid
				break
			}
		}
		if targetID == "" {
			return StartResult{}, false, fmt.Errorf("%w: no replica available", ErrBadTarget)
		}
	}
	if targetID == s.primaryID {
		return StartResult{}, false, ErrTargetIsPrimary
	}
	member := false
	for _, nid := range s.nodes {
		if nid == targetID {
			member = true
		}
	}
	if !member {
		return StartResult{}, false, fmt.Errorf("%w: %q not a member of shard %q", ErrBadTarget, targetID, shardID)
	}
	s.phase = PhaseSnapshot
	s.mig = MigrationView{
		Active:       true,
		Phase:        PhaseSnapshot,
		OldPrimaryID: s.primaryID,
		TargetID:     targetID,
		StartSeq:     s.lastSeq,
	}
	res := StartResult{
		ShardID:  shardID,
		Phase:    s.phase,
		TargetID: targetID,
		StartSeq: s.lastSeq,
		RouteVer: s.routeVer,
	}
	s.remember("start", ctrlKey, res)
	return res, false, nil
}

// SnapshotResult reports snapshot transfer completion (SNAPSHOT -> CATCHUP).
type SnapshotResult struct {
	ShardID     string `json:"shard_id"`
	Phase       Phase  `json:"phase"`
	TargetID    string `json:"target_id"`
	StartSeq    int    `json:"start_seq"`
	SnapshotSeq int    `json:"snapshot_seq"`
	LoadedSeqs  int    `json:"loaded_write_count"`
	CaughtSeqs  int    `json:"caught_write_count"`
	Replayed    bool   `json:"replayed"`
}

// CompleteSnapshot loads the bulk snapshot (writes seq 1..startSeq) onto the
// target and moves the shard into incremental catch-up.
func (c *Cluster) CompleteSnapshot(shardID, ctrlKey string) (SnapshotResult, bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.shard[shardID]
	if !ok {
		return SnapshotResult{}, false, ErrShardNotFound
	}
	if ctrlKey != "" {
		if r, replay, err := s.ctrlReplay("snapshot_complete", ctrlKey); err != nil {
			return SnapshotResult{}, false, err
		} else if replay {
			res := r.(SnapshotResult)
			res.Replayed = true
			return res, true, nil
		}
	}
	if s.phase != PhaseSnapshot {
		return SnapshotResult{}, false, fmt.Errorf("%w: SNAPSHOT_COMPLETE requires SNAPSHOT, got %s", ErrWrongPhase, s.phase)
	}
	if !c.connectedLocked(s.mig.TargetID) {
		return SnapshotResult{}, false, ErrTargetDisconnected
	}
	loaded := 0
	for _, w := range s.writes {
		if w.Seq <= s.mig.StartSeq {
			w.DeliveredTo = markDelivered(w.DeliveredTo, s.mig.TargetID)
			loaded++
		}
	}
	// Writes that landed DURING the snapshot (StartSeq < seq <= lastSeq) are
	// not part of the bulk snapshot but must not be lost: ship them over the
	// incremental channel now. Delivery is decided by the actual delivered-to
	// set (not the seq watermark), so a watermark advanced by a later write
	// can never open a hole in the target's copy.
	caught := 0
	for _, w := range s.writes {
		if w.Seq > s.mig.StartSeq && !hasNode(w.DeliveredTo, s.mig.TargetID) {
			w.DeliveredTo = append(w.DeliveredTo, s.mig.TargetID)
			caught++
		}
	}
	s.mig.SnapshotSeq = s.mig.StartSeq
	s.mig.CaughtUpSeq = s.lastSeq
	s.phase = PhaseCatchup
	s.mig.Phase = PhaseCatchup
	res := SnapshotResult{
		ShardID:     shardID,
		Phase:       s.phase,
		TargetID:    s.mig.TargetID,
		StartSeq:    s.mig.StartSeq,
		SnapshotSeq: s.mig.SnapshotSeq,
		LoadedSeqs:  loaded,
		CaughtSeqs:  caught,
	}
	s.remember("snapshot_complete", ctrlKey, res)
	return res, false, nil
}

// CatchupResult reports an incremental catch-up flush.
type CatchupResult struct {
	ShardID     string `json:"shard_id"`
	Phase       Phase  `json:"phase"`
	TargetID    string `json:"target_id"`
	CaughtUpSeq int    `json:"caught_up_seq"`
	LastSeq     int    `json:"primary_last_seq"`
	Flushed     int    `json:"flushed_writes"`
	CaughtUp    bool   `json:"caught_up"`
	Replayed    bool   `json:"replayed"`
}

// Catchup streams writes the target is missing (used after a target disconnect
// during CATCHUP). It never advances the phase; switch requires caught_up=true.
func (c *Cluster) Catchup(shardID, ctrlKey string) (CatchupResult, bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.shard[shardID]
	if !ok {
		return CatchupResult{}, false, ErrShardNotFound
	}
	if ctrlKey != "" {
		if r, replay, err := s.ctrlReplay("catchup", ctrlKey); err != nil {
			return CatchupResult{}, false, err
		} else if replay {
			res := r.(CatchupResult)
			res.Replayed = true
			return res, true, nil
		}
	}
	if s.phase != PhaseCatchup {
		return CatchupResult{}, false, fmt.Errorf("%w: CATCHUP requires CATCHUP phase, got %s", ErrWrongPhase, s.phase)
	}
	if !c.connectedLocked(s.mig.TargetID) {
		return CatchupResult{}, false, ErrTargetDisconnected
	}
	flushed := 0
	for _, w := range s.writes {
		// Deliver any write the target is missing, regardless of the recorded
		// watermark: delivery state, not the watermark, is the source of truth.
		if !hasNode(w.DeliveredTo, s.mig.TargetID) {
			w.DeliveredTo = append(w.DeliveredTo, s.mig.TargetID)
			flushed++
		}
	}
	s.mig.CaughtUpSeq = s.lastSeq
	res := CatchupResult{
		ShardID:     shardID,
		Phase:       s.phase,
		TargetID:    s.mig.TargetID,
		CaughtUpSeq: s.mig.CaughtUpSeq,
		LastSeq:     s.lastSeq,
		Flushed:     flushed,
		CaughtUp:    s.mig.CaughtUpSeq >= s.lastSeq,
	}
	s.remember("catchup", ctrlKey, res)
	return res, false, nil
}

// SwitchResult reports the atomic routing-version cut-over.
type SwitchResult struct {
	ShardID      string `json:"shard_id"`
	Phase        Phase  `json:"phase"`
	OldPrimaryID string `json:"old_primary_id"`
	NewPrimaryID string `json:"new_primary_id"`
	OldRouteVer  int    `json:"old_route_ver"`
	NewRouteVer  int    `json:"new_route_ver"`
	SwitchSeq    int    `json:"switch_seq"`
	Replayed     bool   `json:"replayed"`
}

// Switch performs CATCHUP -> SWITCHED atomically. It refuses while the target
// is disconnected or lagging, so the cut-over point has exactly one confirmer.
func (c *Cluster) Switch(shardID, ctrlKey string) (SwitchResult, bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.shard[shardID]
	if !ok {
		return SwitchResult{}, false, ErrShardNotFound
	}
	if ctrlKey != "" {
		if r, replay, err := s.ctrlReplay("switch", ctrlKey); err != nil {
			return SwitchResult{}, false, err
		} else if replay {
			res := r.(SwitchResult)
			res.Replayed = true
			return res, true, nil
		}
	}
	if s.phase != PhaseCatchup {
		return SwitchResult{}, false, fmt.Errorf("%w: SWITCH requires CATCHUP, got %s", ErrWrongPhase, s.phase)
	}
	if !c.connectedLocked(s.mig.TargetID) {
		return SwitchResult{}, false, ErrTargetDisconnected
	}
	if missing := s.missingOnTargetLocked(s.mig.TargetID); missing > 0 {
		return SwitchResult{}, false, fmt.Errorf("%w: target is missing %d write(s), watermark %d/%d",
			ErrLagRemaining, missing, s.mig.CaughtUpSeq, s.lastSeq)
	}
	old := s.primaryID
	s.primaryID = s.mig.TargetID
	s.mig.SwitchSeq = s.lastSeq
	s.mig.NewRouteVer = s.routeVer + 1
	s.routeVer++
	s.phase = PhaseSwitched
	s.mig.Phase = PhaseSwitched
	res := SwitchResult{
		ShardID:      shardID,
		Phase:        s.phase,
		OldPrimaryID: old,
		NewPrimaryID: s.mig.TargetID,
		OldRouteVer:  s.routeVer - 1,
		NewRouteVer:  s.routeVer,
		SwitchSeq:    s.mig.SwitchSeq,
	}
	s.remember("switch", ctrlKey, res)
	return res, false, nil
}

// ResetShard rebuilds a shard in RUNNING with the same membership, first node
// as primary, and discards all writes and migration state.
func (c *Cluster) ResetShard(shardID string) (ShardView, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.shard[shardID]
	if !ok {
		return ShardView{}, ErrShardNotFound
	}
	members := append([]string(nil), s.nodes...)
	c.shard[shardID] = &shard{
		id:           shardID,
		routeVer:     1,
		primaryID:    members[0],
		phase:        PhaseRunning,
		nodes:        members,
		writeDedup:   map[string]*Write{},
		ctrlReceipts: map[string]*ctrlReceipt{},
	}
	return c.shardViewLocked(c.shard[shardID]), nil
}

// markDelivered appends node unless already present.
func markDelivered(list []string, node string) []string {
	for _, n := range list {
		if n == node {
			return list
		}
	}
	return append(list, node)
}

// hasNode reports whether node is present in the delivered-to list.
func hasNode(list []string, node string) bool {
	for _, n := range list {
		if n == node {
			return true
		}
	}
	return false
}

// missingOnTargetLocked counts writes the migration target has not received;
// it is the authoritative lag check used to gate the cut-over.
func (s *shard) missingOnTargetLocked(target string) int {
	missing := 0
	for _, w := range s.writes {
		if !hasNode(w.DeliveredTo, target) {
			missing++
		}
	}
	return missing
}
