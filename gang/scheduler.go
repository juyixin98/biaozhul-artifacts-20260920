package gang

import (
	"context"
	"fmt"
	"sort"
	"sync"
	"time"
)

// Clock returns the current time as unix milliseconds. Overridable in tests.
type Clock func() int64

// Scheduler is the in-memory gang scheduler. All state is guarded by one
// mutex; every public operation is linearized, so reserve/commit can never
// interleave into a partial allocation.
type Scheduler struct {
	mu sync.Mutex

	nodes map[string]*Node
	gangs map[string]*Gang
	plans map[string]*Plan

	// queued holds gang ids in FIFO submission order that are waiting for a
	// reservation attempt.
	queued []string
	seq    int

	generation int64 // bumps on every state mutation (observability only)
	now        Clock
	defaultTTL int64 // millis
	sweep      time.Duration

	cancel context.CancelFunc
	done   chan struct{}
}

// Option configures a Scheduler.
type Option func(*Scheduler)

// WithClock injects a clock (tests).
func WithClock(c Clock) Option {
	return func(s *Scheduler) { s.now = c }
}

// WithDefaultTTL sets the reservation TTL used when a request omits one.
func WithDefaultTTL(d time.Duration) Option {
	return func(s *Scheduler) { s.defaultTTL = d.Milliseconds() }
}

// WithSweepInterval sets how often the background reaper scans for expired
// reservations.
func WithSweepInterval(d time.Duration) Option {
	return func(s *Scheduler) { s.sweep = d }
}

// NewScheduler builds a scheduler with no nodes.
func NewScheduler(opts ...Option) *Scheduler {
	s := &Scheduler{
		nodes:      map[string]*Node{},
		gangs:      map[string]*Gang{},
		plans:      map[string]*Plan{},
		now:        func() int64 { return time.Now().UnixMilli() },
		defaultTTL: (15 * time.Second).Milliseconds(),
		sweep:      200 * time.Millisecond,
	}
	for _, o := range opts {
		o(s)
	}
	return s
}

// Start launches the reservation-expiry reaper. Safe to call once.
func (s *Scheduler) Start(ctx context.Context) {
	ctx, cancel := context.WithCancel(ctx)
	s.cancel = cancel
	s.done = make(chan struct{})
	go func() {
		defer close(s.done)
		t := time.NewTicker(s.sweep)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				s.ExpireDue(s.now())
			}
		}
	}()
}

// Stop halts the background reaper.
func (s *Scheduler) Stop() {
	if s.cancel != nil {
		s.cancel()
		<-s.done
	}
}

func (s *Scheduler) bump() { s.generation++ }

// AddNode registers a node. It starts online.
func (s *Scheduler) AddNode(id, zone string, labels map[string]string, capacity int) error {
	if id == "" {
		return fmt.Errorf("%w: node id is required", ErrInvalid)
	}
	if capacity <= 0 {
		capacity = 1
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.nodes[id]; ok {
		return fmt.Errorf("%w: node %q already exists", ErrConflict, id)
	}
	s.nodes[id] = &Node{
		ID:       id,
		Zone:     zone,
		Labels:   cloneLabels(labels),
		Capacity: capacity,
		Online:   true,
		version:  1,
	}
	s.bump()
	s.scheduleLocked("")
	return nil
}

// SetNodeOnline toggles a node's online state.
//
// Offlining a node with running tasks is rejected (409). Offlining a node
// that only holds reservations is allowed: every plan touching the node is
// invalidated atomically and ALL of its held nodes are released, so no
// reservation is left half alive. A later commit of such a plan fails 410.
func (s *Scheduler) SetNodeOnline(id string, online bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	n, ok := s.nodes[id]
	if !ok {
		return fmt.Errorf("%w: node %q", ErrNotFound, id)
	}
	if n.Online == online {
		return nil
	}
	if !online {
		if len(n.runningBy) > 0 {
			return fmt.Errorf("%w: node %q still runs %d task slot(s)", ErrConflict, id, len(n.runningBy))
		}
		// Snapshot the plans holding this node, then invalidate each in full.
		seen := map[string]bool{}
		for _, pid := range n.reservedBy {
			if !seen[pid] {
				seen[pid] = true
			}
		}
		for pid := range seen {
			s.invalidatePlanLocked(s.plans[pid], planAborted)
		}
	}
	n.Online = online
	n.version++
	s.bump()
	s.scheduleLocked("")
	return nil
}

// SubmitGang validates a gang spec and immediately attempts an atomic
// reservation. The returned gang is "reserved" (with a plan) or "waiting".
func (s *Scheduler) SubmitGang(spec GangSpec) (*Gang, *Plan, error) {
	if err := validateSpec(spec); err != nil {
		return nil, nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.gangs[spec.ID]; ok {
		return nil, nil, fmt.Errorf("%w: gang %q already exists", ErrConflict, spec.ID)
	}
	for i := range spec.Tasks {
		if spec.Tasks[i].Name == "" {
			spec.Tasks[i].Name = fmt.Sprintf("task-%d", i)
		}
	}
	g := &Gang{Spec: spec, Status: statusWaiting}
	s.gangs[spec.ID] = g
	s.queued = append(s.queued, spec.ID)
	s.bump()
	plan := s.scheduleLocked("")
	// scheduleLocked returns one arbitrary newly created plan; find this
	// gang's plan explicitly.
	if g.ActivePlanID != "" {
		plan = s.plans[g.ActivePlanID]
	}
	return g, plan, nil
}

// RetryReserve asks the scheduler to try reserving again for a waiting gang.
func (s *Scheduler) RetryReserve(gangID string) (*Gang, *Plan, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	g, ok := s.gangs[gangID]
	if !ok {
		return nil, nil, fmt.Errorf("%w: gang %q", ErrNotFound, gangID)
	}
	if g.Status != statusWaiting {
		return nil, nil, fmt.Errorf("%w: gang %q is %s, not waiting", ErrConflict, gangID, g.Status)
	}
	// A gang that released its reservation left the queue; re-enter it at
	// the tail for the explicit retry.
	if !contains(s.queued, gangID) {
		s.queued = append(s.queued, gangID)
	}
	s.scheduleLocked("")
	var p *Plan
	if g.ActivePlanID != "" {
		p = s.plans[g.ActivePlanID]
	}
	return g, p, nil
}

// CommitPlan turns a reservation into running tasks. It is the optimistic
// concurrency checkpoint: the echoed version must match and every chosen
// node must be unchanged and online. Any mismatch aborts the WHOLE plan
// (releasing every held node) — a gang never partially starts.
func (s *Scheduler) CommitPlan(gangID, planID string, version int64) (*Gang, *Plan, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	g, p, err := s.lookupPlan(gangID, planID)
	if err != nil {
		return nil, nil, err
	}
	switch p.Status {
	case planReserved:
		// proceed below
	case planCommitted:
		return nil, nil, fmt.Errorf("%w: plan %q already committed", ErrConflict, p.ID)
	default:
		return nil, nil, fmt.Errorf("%w: plan %q is %s", ErrExpired, p.ID, p.Status)
	}
	if version != p.Version {
		// Stale/unknown optimistic token. Abort fully rather than risk a
		// commit against state the client never saw.
		s.invalidatePlanLocked(p, planAborted)
		return nil, nil, fmt.Errorf("%w: version mismatch for plan %q (got %d, want %d)", ErrConflict, p.ID, version, p.Version)
	}
	if p.expiresAt <= s.now() {
		s.invalidatePlanLocked(p, planExpired)
		s.markGangExpiredLocked(g)
		return nil, nil, fmt.Errorf("%w: plan %q ttl elapsed", ErrExpired, p.ID)
	}
	for _, nid := range p.Nodes {
		n := s.nodes[nid]
		if n == nil || !n.Online {
			s.invalidatePlanLocked(p, planAborted)
			return nil, nil, fmt.Errorf("%w: node %q is offline or gone", ErrConflict, nid)
		}
		if n.version != p.nodeVersions[nid] {
			s.invalidatePlanLocked(p, planAborted)
			return nil, nil, fmt.Errorf("%w: node %q changed since reservation", ErrConflict, nid)
		}
	}

	// All checks passed under the lock: convert reserved slots to running
	// slots for every node at once.
	counts := map[string]int{}
	for _, nid := range p.Nodes {
		counts[nid]++
	}
	for nid, c := range counts {
		n := s.nodes[nid]
		n.reservedBy = removeEntries(n.reservedBy, p.ID, c)
		for i := 0; i < c; i++ {
			n.runningBy = append(n.runningBy, g.Spec.ID)
		}
		n.version++
	}
	p.Status = planCommitted
	p.committedAt = s.now()
	g.Status = statusRunning
	s.bump()
	return g, p, nil
}

// ReleasePlan cancels a reservation and returns the gang to the waiting
// queue (at its tail). Freed nodes are offered to other waiting gangs
// immediately, but not to the releasing gang itself — use RetryReserve.
func (s *Scheduler) ReleasePlan(gangID, planID string) (*Gang, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	g, p, err := s.lookupPlan(gangID, planID)
	if err != nil {
		return nil, err
	}
	if p.Status != planReserved {
		return nil, fmt.Errorf("%w: plan %q is %s", ErrConflict, p.ID, p.Status)
	}
	s.invalidatePlanLocked(p, planReleased)
	g.Status = statusWaiting
	// invalidatePlanLocked requeued the gang for TTL/topology aborts; a
	// deliberate release leaves the queue entirely — freed nodes go to OTHER
	// waiters, and this gang reserves again only on explicit POST .../retry.
	s.queued = removeString(s.queued, g.Spec.ID)
	s.bump()
	s.scheduleLocked(g.Spec.ID) // let other waiters use the freed nodes
	return g, nil
}

// CompleteGang finishes a running gang and frees its nodes.
func (s *Scheduler) CompleteGang(gangID string) (*Gang, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	g, ok := s.gangs[gangID]
	if !ok {
		return nil, fmt.Errorf("%w: gang %q", ErrNotFound, gangID)
	}
	if g.Status != statusRunning {
		return nil, fmt.Errorf("%w: gang %q is %s, not running", ErrConflict, gangID, g.Status)
	}
	p := s.plans[g.ActivePlanID]
	for _, nid := range p.Nodes {
		n := s.nodes[nid]
		n.runningBy = removeEntries(n.runningBy, gangID, 1)
		n.version++
	}
	p.Status = planCommitted // terminal; committedAt already set
	g.Status = statusSucceeded
	g.ActivePlanID = ""
	s.bump()
	s.scheduleLocked("")
	return g, nil
}

// ExpireDue invalidates every reserved plan whose TTL has elapsed by now.
// Called by the background reaper; also directly in tests.
func (s *Scheduler) ExpireDue(now int64) []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	var expired []string
	for _, p := range s.plans {
		if p.Status == planReserved && p.expiresAt <= now {
			g := s.gangs[p.GangID]
			s.invalidatePlanLocked(p, planExpired)
			s.markGangExpiredLocked(g)
			expired = append(expired, p.ID)
		}
	}
	if len(expired) > 0 {
		sort.Strings(expired)
		s.bump()
	}
	return expired
}

func (s *Scheduler) markGangExpiredLocked(g *Gang) {
	g.Status = statusExpired
	g.ActivePlanID = ""
	s.queued = removeString(s.queued, g.Spec.ID)
}

func (s *Scheduler) lookupPlan(gangID, planID string) (*Gang, *Plan, error) {
	g, ok := s.gangs[gangID]
	if !ok {
		return nil, nil, fmt.Errorf("%w: gang %q", ErrNotFound, gangID)
	}
	p, ok := s.plans[planID]
	if !ok {
		return nil, nil, fmt.Errorf("%w: plan %q", ErrNotFound, planID)
	}
	// A plan always knows its gang even after the gang's ActivePlanID has
	// moved on (expiry/abort), so commit can still return the terminal
	// status instead of a bogus ownership conflict.
	if p.GangID != gangID {
		return nil, nil, fmt.Errorf("%w: plan %q does not belong to gang %q", ErrConflict, planID, gangID)
	}
	return g, p, nil
}

// invalidatePlanLocked releases every node held by a plan and marks it.
// Caller is responsible for updating the gang's status/queue position.
func (s *Scheduler) invalidatePlanLocked(p *Plan, st planStatus) {
	if p.Status != planReserved {
		return
	}
	for _, nid := range p.Nodes {
		n := s.nodes[nid]
		if n == nil {
			continue
		}
		n.reservedBy = removeEntries(n.reservedBy, p.ID, 1)
		n.version++
	}
	p.Status = st
	if g := s.gangs[p.GangID]; g != nil && g.ActivePlanID == p.ID {
		g.ActivePlanID = ""
		if g.Status == statusReserved || g.Status == statusRunning {
			g.Status = statusWaiting
			// Re-enter the queue at the front: this gang was waiting before
			// any gang submitted meanwhile.
			if !contains(s.queued, g.Spec.ID) {
				s.queued = append([]string{g.Spec.ID}, s.queued...)
			}
		}
	}
	s.bump()
}

// scheduleLocked walks the waiting queue and reserves every gang that can
// get all its nodes right now. Gangs that cannot fully fit keep waiting and
// hold nothing. skipGang is a gang id excluded from this sweep.
func (s *Scheduler) scheduleLocked(skipGang string) *Plan {
	var made *Plan
	for _, gid := range append([]string(nil), s.queued...) {
		if gid == skipGang {
			continue
		}
		g := s.gangs[gid]
		if g == nil || g.Status != statusWaiting {
			s.queued = removeString(s.queued, gid)
			continue
		}
		nodes, nodeVersions, ok := s.assignNodesLocked(g)
		if !ok {
			continue // all-or-nothing: keep waiting, hold nothing
		}
		s.seq++
		pid := fmt.Sprintf("p%d", s.seq)
		ttl := int64(g.Spec.TTLMillis)
		if ttl <= 0 {
			ttl = s.defaultTTL
		}
		p := &Plan{
			ID:           pid,
			GangID:       gid,
			Nodes:        nodes,
			nodeVersions: nodeVersions,
			Status:       planReserved,
			expiresAt:    s.now() + ttl,
		}
		// Hold the chosen slots, bumping each node's version, then issue the
		// optimistic token against the post-hold versions — those are what
		// commit will observe.
		for _, nid := range nodes {
			n := s.nodes[nid]
			n.reservedBy = append(n.reservedBy, pid)
			n.version++
		}
		p.nodeVersions = map[string]int64{}
		for _, nid := range nodes {
			p.nodeVersions[nid] = s.nodes[nid].version
		}
		p.Version = s.generation + 1
		s.plans[pid] = p
		g.Status = statusReserved
		g.ActivePlanID = pid
		s.queued = removeString(s.queued, gid)
		s.bump()
		if made == nil {
			made = p
		}
	}
	return made
}

// assignNodesLocked greedily picks one node per task. Returns ok=false
// without holding anything if any single task cannot be placed.
func (s *Scheduler) assignNodesLocked(g *Gang) ([]string, map[string]int64, bool) {
	spec := g.Spec
	usedValues := map[string]map[string]bool{} // anti-affinity key -> used label values
	// tentative counts slots this gang's earlier tasks just claimed; they are
	// not in reservedBy yet, so capacity checks must include them or one
	// capacity-1 node could be picked for every task.
	tentative := map[string]int{}
	nodes := make([]string, 0, len(spec.Tasks))

	for _, task := range spec.Tasks {
		candidates := make([]*Node, 0)
		for _, n := range s.nodes {
			if !n.Online {
				continue
			}
			if len(n.reservedBy)+len(n.runningBy)+tentative[n.ID] >= n.Capacity {
				continue
			}
			if !labelsMatch(n.Labels, task.NodeSelector) {
				continue
			}
			if spec.AntiAffinity != "" {
				v := affinityValue(n, spec.AntiAffinity)
				if usedValues[spec.AntiAffinity] != nil && usedValues[spec.AntiAffinity][v] {
					continue // another task already lands on a node with this value
				}
			}
			candidates = append(candidates, n)
		}
		if len(candidates) == 0 {
			return nil, nil, false
		}
		sort.Slice(candidates, func(i, j int) bool { return candidates[i].ID < candidates[j].ID })
		chosen := candidates[0]
		nodes = append(nodes, chosen.ID)
		tentative[chosen.ID]++
		if spec.AntiAffinity != "" {
			v := affinityValue(chosen, spec.AntiAffinity)
			if usedValues[spec.AntiAffinity] == nil {
				usedValues[spec.AntiAffinity] = map[string]bool{}
			}
			usedValues[spec.AntiAffinity][v] = true
		}
	}
	// Versions are recorded after holding (caller bumps), so return nil here.
	return nodes, nil, true
}

func validateSpec(spec GangSpec) error {
	if spec.ID == "" {
		return fmt.Errorf("%w: gang id is required", ErrInvalid)
	}
	if spec.MinNodes < 1 {
		return fmt.Errorf("%w: min_nodes must be >= 1", ErrInvalid)
	}
	if len(spec.Tasks) != spec.MinNodes {
		return fmt.Errorf("%w: len(tasks)=%d must equal min_nodes=%d (one node per task)", ErrInvalid, len(spec.Tasks), spec.MinNodes)
	}
	if spec.TTLMillis < 0 {
		return fmt.Errorf("%w: ttl_millis must be > 0 or omitted", ErrInvalid)
	}
	names := map[string]bool{}
	for i, t := range spec.Tasks {
		key := t.Name
		if key == "" {
			key = fmt.Sprintf("task-%d", i)
		}
		if names[key] {
			return fmt.Errorf("%w: duplicate task name %q", ErrInvalid, key)
		}
		names[key] = true
	}
	return nil
}

func labelsMatch(labels, selector map[string]string) bool {
	for k, v := range selector {
		if labels[k] != v {
			return false
		}
	}
	return true
}

// affinityValue resolves an anti-affinity key on a node. It is a normal label
// lookup, except the built-in key "zone" falls back to the node's zone field
// when no such label is present.
func affinityValue(n *Node, key string) string {
	if v, ok := n.Labels[key]; ok {
		return v
	}
	if key == "zone" {
		return n.Zone
	}
	return ""
}

func cloneLabels(m map[string]string) map[string]string {
	if len(m) == 0 {
		return map[string]string{}
	}
	c := make(map[string]string, len(m))
	for k, v := range m {
		c[k] = v
	}
	return c
}

func removeEntries(s []string, val string, count int) []string {
	out := s[:0]
	removed := 0
	for _, v := range s {
		if v == val && removed < count {
			removed++
			continue
		}
		out = append(out, v)
	}
	return out
}

func removeString(s []string, v string) []string {
	out := s[:0]
	for _, x := range s {
		if x != v {
			out = append(out, x)
		}
	}
	return out
}

func contains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}
