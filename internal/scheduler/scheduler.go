// Package scheduler implements an in-memory gang scheduler.
//
// A gang is a set of tasks that must be scheduled atomically: either every
// task gets a node at once, or the whole gang waits. Scheduling is a two
// phase protocol:
//
//  1. Reserve  — the planner picks nodes and HOLDS slots on them for a bounded
//     TTL (the "reservation window"). If not every task fits, the
//     gang enters a FIFO WAITING queue and nothing is held.
//  2. Commit   — the client submits the plan with the per-node versions it
//     observed. The versions are re-checked under the scheduler
//     lock, node capacity/state is re-validated, and only then are
//     slots converted from held to running. Any failed check
//     releases the whole reservation (no partial start, no leak).
//
// Held reservations that are never committed expire via a reaper goroutine.
package scheduler

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"
)

// Statuses.
const (
	NodeOnline  = "ONLINE"
	NodeOffline = "OFFLINE"

	GangWaiting  = "WAITING"
	GangHeld     = "HELD"
	GangRunning  = "RUNNING"
	GangFailed   = "FAILED"   // validation failed on commit, or reservation expired
	GangComplete = "COMPLETE" // voluntarily released by the client
)

var (
	ErrNotFound      = errors.New("not found")
	ErrAlreadyExists = errors.New("already exists")
	ErrInvalidState  = errors.New("invalid state")
)

// ValidationError describes a malformed request.
type ValidationError struct{ Msg string }

func (e *ValidationError) Error() string { return e.Msg }

// ConflictError describes a request that is well-formed but cannot be applied
// to the current state (unschedulable, version mismatch, TTL expired, ...).
type ConflictError struct {
	Code   string // "UNSATISFIABLE", "VERSION_MISMATCH", "RESERVATION_EXPIRED", "STATE"
	Reason string
}

func (e *ConflictError) Error() string { return e.Code + ": " + e.Reason }

// Selector is a label requirement: node labels must contain Key with a value
// in Values (set semantics), i.e. key IN (v1, v2, ...).
type Selector struct {
	Key    string
	Values []string
}

// TaskSpec is one member of a gang.
type TaskSpec struct {
	ID    string              `json:"id"`
	Slots int                 `json:"slots"`
	Match map[string]string   `json:"match_labels,omitempty"` // node must carry all these label pairs
	In    map[string][]string `json:"in_labels,omitempty"`    // node label value must be in the list
}

// NodeView is a read-only snapshot of a node.
type NodeView struct {
	Name     string            `json:"name"`
	Labels   map[string]string `json:"labels"`
	Capacity int               `json:"capacity"`
	Reserved int               `json:"reserved"` // held by uncommitted reservations
	Running  int               `json:"running"`  // committed
	Status   string            `json:"status"`
	Version  int64             `json:"version"`
}

// Assignment pairs a task id with the node it landed on.
type Assignment struct {
	TaskID   string `json:"task_id"`
	NodeName string `json:"node_name"`
	Slots    int    `json:"slots"`
}

// ReservationView is the snapshot returned for a HELD gang.
type ReservationView struct {
	ID           int64            `json:"id"`
	ExpiresAt    time.Time        `json:"expires_at"`
	TTLMillis    int64            `json:"ttl_ms"`
	Assignments  []Assignment     `json:"assignments"`
	NodeVersions map[string]int64 `json:"node_versions"` // echo these on commit
}

// GangView is a read-only snapshot of a gang.
type GangView struct {
	ID          string           `json:"id"`
	Status      string           `json:"status"`
	Tasks       []TaskSpec       `json:"tasks"`
	Distinct    bool             `json:"distinct_nodes"`
	Reservation *ReservationView `json:"reservation,omitempty"`
	Running     []Assignment     `json:"running,omitempty"`
	Reason      string           `json:"reason,omitempty"`
	CreatedAt   time.Time        `json:"created_at"`
	UpdatedAt   time.Time        `json:"updated_at"`
}

// StateView is a full snapshot, useful for tests/debug.
type StateView struct {
	Nodes []NodeView `json:"nodes"`
	Gangs []GangView `json:"gangs"`
}

type node struct {
	name     string
	labels   map[string]string
	capacity int
	reserved int
	running  int
	status   string
	version  int64
}

func (n *node) free() int { return n.capacity - n.running - n.reserved }

type reservation struct {
	id          int64
	expiresAt   time.Time
	ttl         time.Duration
	assignments []Assignment // one entry per task, in task order
}

type gang struct {
	id          string
	tasks       []TaskSpec
	distinct    bool
	ttl         time.Duration
	status      string
	reservation *reservation
	running     []Assignment
	reason      string
	createdAt   time.Time
	updatedAt   time.Time
}

// Config holds scheduler tunables.
type Config struct {
	DefaultTTL time.Duration // reservation window when a gang does not specify one
	ReapEvery  time.Duration // reaper tick
	Now        func() time.Time
}

// Scheduler is safe for concurrent use. All state transitions happen under
// one mutex; reservation/commit never interleave with each other or with
// node lifecycle changes.
type Scheduler struct {
	mu sync.Mutex

	cfg Config
	now func() time.Time

	nodes map[string]*node
	gangs map[string]*gang
	order []string // insertion order of gangs

	waiting []string // FIFO queue of WAITING gang ids

	nextReservationID int64

	stopped chan struct{}
	wg      sync.WaitGroup
}

// New constructs a scheduler and starts the expiration reaper.
func New(cfg Config) *Scheduler {
	if cfg.DefaultTTL <= 0 {
		cfg.DefaultTTL = 5 * time.Second
	}
	if cfg.ReapEvery <= 0 {
		cfg.ReapEvery = 50 * time.Millisecond
	}
	now := cfg.Now
	if now == nil {
		now = time.Now
	}
	s := &Scheduler{
		cfg:     cfg,
		now:     now,
		nodes:   map[string]*node{},
		gangs:   map[string]*gang{},
		stopped: make(chan struct{}),
	}
	s.wg.Add(1)
	go s.reapLoop()
	return s
}

// Stop terminates the reaper.
func (s *Scheduler) Stop() {
	close(s.stopped)
	s.wg.Wait()
}

// ---------------------------------------------------------------------------
// Nodes
// ---------------------------------------------------------------------------

// AddNode registers a node. Capacity must be positive.
func (s *Scheduler) AddNode(name string, labels map[string]string, capacity int) error {
	if strings.TrimSpace(name) == "" {
		return &ValidationError{"node name must not be empty"}
	}
	if capacity <= 0 {
		return &ValidationError{"capacity must be positive"}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.nodes[name]; ok {
		return ErrAlreadyExists
	}
	s.nodes[name] = &node{
		name:     name,
		labels:   cloneLabels(labels),
		capacity: capacity,
		status:   NodeOnline,
		version:  1,
	}
	s.pumpWaitingLocked()
	return nil
}

// SetNodeStatus ONLINE/OFFLINE. A node bump version on every change so that
// clients holding an old plan fail their commit version check.
func (s *Scheduler) SetNodeStatus(name, status string) error {
	if status != NodeOnline && status != NodeOffline {
		return &ValidationError{"status must be ONLINE or OFFLINE"}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	n, ok := s.nodes[name]
	if !ok {
		return ErrNotFound
	}
	if n.status == status {
		return nil
	}
	n.status = status
	n.version++
	if status == NodeOnline {
		// Capacity may have become available.
		s.pumpWaitingLocked()
	}
	// When going OFFLINE we do NOT forcibly cancel HELD reservations: their
	// slots are still accounted (no leak) and the commit-time revalidation
	// rejects them. The reservation TTL bounds how long the hold survives.
	return nil
}

// ---------------------------------------------------------------------------
// Gang lifecycle
// ---------------------------------------------------------------------------

// SubmitGang creates a gang and immediately tries to reserve. If all tasks
// fit, the gang becomes HELD and a reservation is returned; otherwise it is
// WAITING (FIFO) and the reservation is nil. ttl <= 0 means DefaultTTL.
func (s *Scheduler) SubmitGang(id string, tasks []TaskSpec, distinctNodes bool, ttl time.Duration) (*GangView, error) {
	if err := validateGang(id, tasks); err != nil {
		return nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.gangs[id]; ok {
		return nil, ErrAlreadyExists
	}
	if ttl <= 0 {
		ttl = s.cfg.DefaultTTL
	}
	now := s.now()
	g := &gang{
		id:        id,
		tasks:     cloneTasks(tasks),
		distinct:  distinctNodes,
		ttl:       ttl,
		status:    GangWaiting,
		createdAt: now,
		updatedAt: now,
	}
	s.gangs[id] = g
	s.order = append(s.order, id)
	s.waiting = append(s.waiting, id)
	s.pumpWaitingLocked()
	return s.gangViewLocked(g), nil
}

// Replan re-attempts scheduling of a WAITING (or FAILED-due-to-unsatisfiable)
// gang. It does not let a gang jump the queue: the full FIFO queue is
// re-pumped in order.
func (s *Scheduler) Replan(gangID string) (*GangView, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	g, ok := s.gangs[gangID]
	if !ok {
		return nil, ErrNotFound
	}
	if g.status == GangHeld || g.status == GangRunning || g.status == GangComplete {
		return nil, &ConflictError{Code: "STATE", Reason: "gang is " + g.status}
	}
	// Only WAITING gangs are in the queue; FAILED gangs must re-enter.
	if g.status == GangFailed {
		g.status = GangWaiting
		g.reason = ""
		g.createdAt = s.now()
		g.updatedAt = g.createdAt
		s.waiting = append(s.waiting, g.id)
	}
	s.pumpWaitingLocked()
	return s.gangViewLocked(g), nil
}

// Commit submits the plan with the node versions the client observed. Every
// check and the whole state transition run under the lock: either the gang
// becomes RUNNING (all tasks at once) or the reservation is released and the
// gang FAILEDs (nothing partially starts).
func (s *Scheduler) Commit(gangID string, expectedNodeVersions map[string]int64) (*GangView, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	g, ok := s.gangs[gangID]
	if !ok {
		return nil, ErrNotFound
	}
	if g.status != GangHeld || g.reservation == nil {
		return nil, &ConflictError{Code: "STATE", Reason: "gang is not HELD (status=" + g.status + ")"}
	}
	r := g.reservation
	now := s.now()
	if !now.Before(r.expiresAt) {
		// Normal path is reaper; this covers the exact race window.
		s.failAndReleaseLocked(g, "RESERVATION_EXPIRED", "reservation expired at "+r.expiresAt.Format(time.RFC3339Nano))
		return nil, &ConflictError{Code: "RESERVATION_EXPIRED", Reason: "reservation expired at " + r.expiresAt.Format(time.RFC3339Nano)}
	}

	// 1. Version check: every node in the plan must still be at the version
	//    the client based its decision on. Going OFFLINE bumps the version,
	//    so a node taken down between reserve and commit is caught here.
	for name, expected := range expectedNodeVersions {
		n, ok := s.nodes[name]
		if !ok {
			s.failAndReleaseLocked(g, "VERSION_MISMATCH", "node "+name+" no longer exists")
			return nil, &ConflictError{Code: "VERSION_MISMATCH", Reason: "node " + name + " no longer exists"}
		}
		if n.version != expected {
			s.failAndReleaseLocked(g, "VERSION_MISMATCH",
				fmt.Sprintf("node %s version %d, plan was based on %d", name, n.version, expected))
			return nil, &ConflictError{Code: "VERSION_MISMATCH", Reason: fmt.Sprintf("stale plan: node %s changed", name)}
		}
	}
	// The client must acknowledge every node in the plan.
	for _, a := range r.assignments {
		if _, ok := expectedNodeVersions[a.NodeName]; !ok {
			return nil, &ValidationError{"expected_node_versions must cover every planned node"}
		}
	}

	// 2. Re-validate the full plan against current reality. This is the
	//    all-or-nothing guard against anything versions did not surface.
	used := map[string]int{} // node -> slots newly required by this gang
	usedNodes := map[string]bool{}
	for _, a := range r.assignments {
		n := s.nodes[a.NodeName]
		if n == nil || n.status != NodeOnline {
			s.failAndReleaseLocked(g, "UNSATISFIABLE", "node "+a.NodeName+" is not ONLINE")
			return nil, &ConflictError{Code: "UNSATISFIABLE", Reason: "node " + a.NodeName + " is not ONLINE"}
		}
		used[a.NodeName] += a.Slots
		usedNodes[a.NodeName] = true
		// Post-commit running on the node must fit. This reservation's own
		// slots move reserved->running; other HELD reservations stay
		// reserved and are already part of the capacity invariant.
		if n.running+used[a.NodeName] > n.capacity {
			s.failAndReleaseLocked(g, "UNSATISFIABLE",
				fmt.Sprintf("node %s has %d free slots, plan needs %d", a.NodeName, n.free(), used[a.NodeName]))
			return nil, &ConflictError{Code: "UNSATISFIABLE", Reason: "node " + a.NodeName + " lost capacity"}
		}
	}
	if g.distinct && len(usedNodes) != len(r.assignments) {
		s.failAndReleaseLocked(g, "UNSATISFIABLE", "distinct-nodes constraint violated")
		return nil, &ConflictError{Code: "UNSATISFIABLE", Reason: "distinct-nodes constraint violated"}
	}
	// Labels are immutable in this implementation; a version mismatch would
	// already have caught node replacement. Re-check anyway for defense.
	taskByID := map[string]TaskSpec{}
	for _, t := range g.tasks {
		taskByID[t.ID] = t
	}
	for _, a := range r.assignments {
		t := taskByID[a.TaskID]
		n := s.nodes[a.NodeName]
		if !labelsMatch(n.labels, t) {
			s.failAndReleaseLocked(g, "UNSATISFIABLE", "node "+a.NodeName+" no longer matches task "+a.TaskID)
			return nil, &ConflictError{Code: "UNSATISFIABLE", Reason: "selector no longer satisfied for task " + a.TaskID}
		}
	}

	// 3. Commit atomically: held -> running. Free capacity is unchanged by
	//    this transfer (reserved drops as running rises), so other plans'
	//    versions are not invalidated.
	for name, slots := range used {
		n := s.nodes[name]
		n.reserved -= slots
		n.running += slots
	}
	g.status = GangRunning
	g.running = append([]Assignment(nil), r.assignments...)
	g.reservation = nil
	g.updatedAt = now
	return s.gangViewLocked(g), nil
}

// ReleaseGang completes a RUNNING gang (or cancels a WAITING/HELD one) and
// frees its slots. Waiting gangs behind it may now fit.
func (s *Scheduler) ReleaseGang(gangID string) (*GangView, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	g, ok := s.gangs[gangID]
	if !ok {
		return nil, ErrNotFound
	}
	switch g.status {
	case GangComplete:
		return s.gangViewLocked(g), nil
	case GangRunning:
		for _, a := range g.running {
			n := s.nodes[a.NodeName]
			if n == nil {
				continue
			}
			n.running -= a.Slots
		}
		g.running = nil
		g.status = GangComplete
		g.updatedAt = s.now()
		s.pumpWaitingLocked()
	case GangHeld:
		s.releaseReservationLocked(g)
		g.status = GangComplete
		g.reason = ""
		g.updatedAt = s.now()
		s.pumpWaitingLocked()
	case GangWaiting:
		s.removeFromWaitingLocked(g.id)
		g.status = GangComplete
		g.updatedAt = s.now()
	case GangFailed:
		// nothing held
		g.status = GangComplete
		g.updatedAt = s.now()
	default:
		return nil, &ConflictError{Code: "STATE", Reason: "cannot release gang in status " + g.status}
	}
	return s.gangViewLocked(g), nil
}

// DeleteGang removes a gang. RUNNING/HELD gangs must be released first.
func (s *Scheduler) DeleteGang(gangID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	g, ok := s.gangs[gangID]
	if !ok {
		return ErrNotFound
	}
	if g.status == GangRunning || g.status == GangHeld {
		return &ConflictError{Code: "STATE", Reason: "release the gang before deleting it"}
	}
	delete(s.gangs, g.id)
	for i, id := range s.order {
		if id == g.id {
			s.order = append(s.order[:i], s.order[i+1:]...)
			break
		}
	}
	s.removeFromWaitingLocked(g.id)
	return nil
}

// GetGang returns one gang snapshot.
func (s *Scheduler) GetGang(gangID string) (*GangView, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	g, ok := s.gangs[gangID]
	if !ok {
		return nil, ErrNotFound
	}
	return s.gangViewLocked(g), nil
}

// Snapshot returns the full state for inspection/tests.
func (s *Scheduler) Snapshot() StateView {
	s.mu.Lock()
	defer s.mu.Unlock()
	v := StateView{Gangs: make([]GangView, 0, len(s.order))}
	names := make([]string, 0, len(s.nodes))
	for name := range s.nodes {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		v.Nodes = append(v.Nodes, s.nodeViewLocked(s.nodes[name]))
	}
	for _, id := range s.order {
		v.Gangs = append(v.Gangs, *s.gangViewLocked(s.gangs[id]))
	}
	return v
}

// ---------------------------------------------------------------------------
// Planning (must hold s.mu)
// ---------------------------------------------------------------------------

// pumpWaitingLocked walks the FIFO queue. Each gang in turn is planned
// against whatever capacity remains; a gang that fits is reserved and leaves
// the queue, a gang that does not stops the pump (FIFO, no starvation).
func (s *Scheduler) pumpWaitingLocked() {
	for len(s.waiting) > 0 {
		id := s.waiting[0]
		g := s.gangs[id]
		if g == nil || g.status != GangWaiting {
			s.waiting = s.waiting[1:]
			continue
		}
		plan, ok := s.tryPlanLocked(g)
		if !ok {
			return // head-of-line blocks
		}
		s.waiting = s.waiting[1:]
		s.applyPlanLocked(g, plan)
	}
}

// tryPlanLocked performs a depth-first best-fit placement. Tasks are tried
// largest-slots-first; candidate nodes for each task are ordered by fewest
// free slots (best fit) then name for determinism.
func (s *Scheduler) tryPlanLocked(g *gang) ([]Assignment, bool) {
	tasks := make([]TaskSpec, len(g.tasks))
	copy(tasks, g.tasks)
	sort.SliceStable(tasks, func(i, j int) bool {
		if tasks[i].Slots != tasks[j].Slots {
			return tasks[i].Slots > tasks[j].Slots
		}
		return tasks[i].ID < tasks[j].ID
	})

	plan := make([]Assignment, len(tasks))
	used := map[string]int{}    // node -> slots already consumed by this plan
	chosen := map[string]bool{} // node already used by this plan

	var dfs func(i int) bool
	dfs = func(i int) bool {
		if i == len(tasks) {
			return true
		}
		t := tasks[i]
		candidates := make([]*node, 0, len(s.nodes))
		for _, n := range s.nodes {
			if n.status != NodeOnline || !labelsMatch(n.labels, t) {
				continue
			}
			if g.distinct && chosen[n.name] {
				continue
			}
			if n.free()-used[n.name] < t.Slots {
				continue
			}
			candidates = append(candidates, n)
		}
		sort.Slice(candidates, func(a, b int) bool {
			fa := candidates[a].free() - used[candidates[a].name]
			fb := candidates[b].free() - used[candidates[b].name]
			if fa != fb {
				return fa < fb
			}
			return candidates[a].name < candidates[b].name
		})
		for _, n := range candidates {
			plan[i] = Assignment{TaskID: t.ID, NodeName: n.name, Slots: t.Slots}
			used[n.name] += t.Slots
			chosen[n.name] = true
			if dfs(i + 1) {
				return true
			}
			used[n.name] -= t.Slots
			if used[n.name] == 0 {
				delete(chosen, n.name)
			}
		}
		return false
	}

	if dfs(0) {
		// Return assignments in the gang's declared task order.
		byTask := map[string]Assignment{}
		for _, a := range plan {
			byTask[a.TaskID] = a
		}
		out := make([]Assignment, 0, len(g.tasks))
		for _, t := range g.tasks {
			out = append(out, byTask[t.ID])
		}
		return out, true
	}
	return nil, false
}

func (s *Scheduler) applyPlanLocked(g *gang, plan []Assignment) {
	now := s.now()
	ttl := g.ttl
	if ttl <= 0 {
		ttl = s.cfg.DefaultTTL
	}
	s.nextReservationID++
	r := &reservation{
		id:          s.nextReservationID,
		expiresAt:   now.Add(ttl),
		ttl:         ttl,
		assignments: append([]Assignment(nil), plan...),
	}
	// Version only moves on structural changes (online/offline). Another
	// gang reserving or committing capacity on the same node does not
	// invalidate this plan: reserved slots are already accounted for.
	for _, a := range plan {
		s.nodes[a.NodeName].reserved += a.Slots
	}
	g.status = GangHeld
	g.reservation = r
	// The reservation window starts fresh at promotion time, regardless of
	// how long the gang spent WAITING.
	g.createdAt = now
	g.updatedAt = now
}

// releaseReservationLocked returns held slots and bumps affected node versions.
func (s *Scheduler) releaseReservationLocked(g *gang) {
	if g.reservation == nil {
		return
	}
	for _, a := range g.reservation.assignments {
		n := s.nodes[a.NodeName]
		if n == nil {
			continue
		}
		n.reserved -= a.Slots
	}
	g.reservation = nil
}

func (s *Scheduler) failAndReleaseLocked(g *gang, code, reason string) {
	s.releaseReservationLocked(g)
	g.status = GangFailed
	g.reason = code + ": " + reason
	g.updatedAt = s.now()
	// Failed gangs leave the waiting queue permanently until Replan. Freeing
	// capacity may let queued gangs proceed.
	s.pumpWaitingLocked()
}

func (s *Scheduler) removeFromWaitingLocked(id string) {
	for i, qid := range s.waiting {
		if qid == id {
			s.waiting = append(s.waiting[:i], s.waiting[i+1:]...)
			return
		}
	}
}

// ---------------------------------------------------------------------------
// Reaper
// ---------------------------------------------------------------------------

func (s *Scheduler) reapLoop() {
	defer s.wg.Done()
	ticker := time.NewTicker(s.cfg.ReapEvery)
	defer ticker.Stop()
	for {
		select {
		case <-s.stopped:
			return
		case <-ticker.C:
			s.reapExpired()
		}
	}
}

// reapExpired fails HELD reservations past their deadline and drops WAITING
// gangs whose wait exceeded their TTL (prevents queue leaks/starvation of
// waiters behind an unschedulable head).
func (s *Scheduler) reapExpired() {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	for _, id := range s.order {
		g := s.gangs[id]
		if g == nil {
			continue
		}
		switch g.status {
		case GangHeld:
			if g.reservation != nil && !now.Before(g.reservation.expiresAt) {
				s.failAndReleaseLocked(g, "RESERVATION_EXPIRED",
					"reservation expired at "+g.reservation.expiresAt.Format(time.RFC3339Nano))
			}
		case GangWaiting:
			// Wait budget: a gang that never got a hold also expires (TTL
			// measured from creation/requeue), keeping the queue bounded.
			if !now.Before(g.createdAt.Add(g.ttl)) {
				s.removeFromWaitingLocked(g.id)
				g.status = GangFailed
				g.reason = "RESERVATION_EXPIRED: wait deadline exceeded"
				g.updatedAt = now
			}
		}
	}
}

// ---------------------------------------------------------------------------
// Matching / validation / views
// ---------------------------------------------------------------------------

func labelsMatch(labels map[string]string, t TaskSpec) bool {
	for k, v := range t.Match {
		if labels[k] != v {
			return false
		}
	}
	for k, vals := range t.In {
		got, ok := labels[k]
		if !ok {
			return false
		}
		found := false
		for _, v := range vals {
			if got == v {
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	return true
}

func validateGang(id string, tasks []TaskSpec) error {
	if strings.TrimSpace(id) == "" {
		return &ValidationError{"gang id must not be empty"}
	}
	if len(tasks) == 0 {
		return &ValidationError{"gang must contain at least one task"}
	}
	seen := map[string]bool{}
	for _, t := range tasks {
		if strings.TrimSpace(t.ID) == "" {
			return &ValidationError{"task id must not be empty"}
		}
		if seen[t.ID] {
			return &ValidationError{"duplicate task id: " + t.ID}
		}
		seen[t.ID] = true
		if t.Slots <= 0 {
			return &ValidationError{"task " + t.ID + " slots must be positive"}
		}
	}
	return nil
}

func cloneLabels(in map[string]string) map[string]string {
	out := make(map[string]string, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func cloneTasks(in []TaskSpec) []TaskSpec {
	out := make([]TaskSpec, len(in))
	for i, t := range in {
		out[i] = TaskSpec{
			ID:    t.ID,
			Slots: t.Slots,
			Match: cloneLabels(t.Match),
			In:    cloneStringLists(t.In),
		}
	}
	return out
}

func cloneStringLists(in map[string][]string) map[string][]string {
	if in == nil {
		return nil
	}
	out := make(map[string][]string, len(in))
	for k, v := range in {
		cp := make([]string, len(v))
		copy(cp, v)
		out[k] = cp
	}
	return out
}

func (s *Scheduler) nodeViewLocked(n *node) NodeView {
	return NodeView{
		Name:     n.name,
		Labels:   cloneLabels(n.labels),
		Capacity: n.capacity,
		Reserved: n.reserved,
		Running:  n.running,
		Status:   n.status,
		Version:  n.version,
	}
}

func (s *Scheduler) reservationViewLocked(r *reservation) *ReservationView {
	if r == nil {
		return nil
	}
	versions := map[string]int64{}
	for _, a := range r.assignments {
		n := s.nodes[a.NodeName]
		if n != nil {
			versions[a.NodeName] = n.version
		}
	}
	return &ReservationView{
		ID:           r.id,
		ExpiresAt:    r.expiresAt,
		TTLMillis:    r.ttl.Milliseconds(),
		Assignments:  append([]Assignment(nil), r.assignments...),
		NodeVersions: versions,
	}
}

func (s *Scheduler) gangViewLocked(g *gang) *GangView {
	v := &GangView{
		ID:        g.id,
		Status:    g.status,
		Tasks:     cloneTasks(g.tasks),
		Distinct:  g.distinct,
		Reason:    g.reason,
		CreatedAt: g.createdAt,
		UpdatedAt: g.updatedAt,
	}
	if g.reservation != nil {
		v.Reservation = s.reservationViewLocked(g.reservation)
	}
	if len(g.running) > 0 {
		v.Running = append([]Assignment(nil), g.running...)
	}
	return v
}
