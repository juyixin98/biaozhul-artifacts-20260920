package scheduler

import (
	"fmt"
	"math/big"
	"sort"
	"strings"
	"sync"
	"time"
)

// TaskSpec is a task submission request.
type TaskSpec struct {
	ID       string    `json:"id"`
	TenantID string    `json:"tenant_id"`
	Request  Resources `json:"request"`
	// Duration is how long the task holds its resources once started.
	Duration Duration `json:"duration"`
}

// TaskView is the externally visible state of one task.
type TaskView struct {
	ID         string     `json:"id"`
	TenantID   string     `json:"tenant_id"`
	Seq        int64      `json:"seq"`
	Status     TaskStatus `json:"status"`
	Request    Resources  `json:"request"`
	Duration   Duration   `json:"duration"`
	StartedAt  *time.Time `json:"started_at,omitempty"`
	FinishedAt *time.Time `json:"finished_at,omitempty"`
}

// TenantView is the externally visible state of one tenant.
type TenantView struct {
	ID            string     `json:"id"`
	Weight        int64      `json:"weight"`
	DominantShare float64    `json:"dominant_share"` // informational, float approximation
	Allocated     Resources  `json:"allocated"`
	Running       []TaskView `json:"running"`
	Waiting       []TaskView `json:"waiting"`
	FinishedCount int        `json:"finished_count"`
	// HeadBlocked is true when the queue head cannot currently start:
	// either it exceeds total cluster capacity (permanent) or it does
	// not fit in the resources currently available (transient).
	HeadBlocked    bool `json:"head_blocked"`
	HeadExceedsCap bool `json:"head_exceeds_capacity"` // permanent block
}

// ClusterSnapshot is a consistent point-in-time view of the scheduler.
type ClusterSnapshot struct {
	Capacity  Resources    `json:"capacity"`
	Used      Resources    `json:"used"`
	Available Resources    `json:"available"`
	Tenants   []TenantView `json:"tenants"`
	Tasks     []TaskView   `json:"tasks"`
	EventSeq  int64        `json:"event_seq"`
}

type task struct {
	spec       TaskSpec
	seq        int64
	status     TaskStatus
	startedAt  *time.Time
	finishedAt *time.Time
}

type tenant struct {
	id      string
	weight  int64
	waiting []*task
	// allocated is the sum of running task requests; kept incrementally.
	allocated     Resources
	running       []*task
	finishedCount int
}

func (t *tenant) head() *task {
	if len(t.waiting) == 0 {
		return nil
	}
	return t.waiting[0]
}

// Scheduler is the DRF scheduler. Zero value is not usable; use New.
type Scheduler struct {
	mu       sync.Mutex
	capacity Resources
	used     Resources
	clock    Clock
	exec     Executor
	store    EventStore

	tenants map[string]*tenant
	tasks   map[string]*task
	seq     int64
}

// Option configures a Scheduler at construction.
type Option func(*Scheduler)

func WithClock(c Clock) Option       { return func(s *Scheduler) { s.clock = c } }
func WithExecutor(e Executor) Option { return func(s *Scheduler) { s.exec = e } }
func WithEventStore(st EventStore) Option {
	return func(s *Scheduler) { s.store = st }
}

// New builds a scheduler for the given fixed cluster capacity.
// By default it uses a RealClock, TimedExecutor and MemoryStore.
func New(capacity Resources, opts ...Option) (*Scheduler, error) {
	if capacity.CPU <= 0 || capacity.Mem <= 0 {
		return nil, fmt.Errorf("%w: capacity must be positive in both dimensions", ErrBadRequest)
	}
	s := &Scheduler{
		capacity: capacity,
		tenants:  make(map[string]*tenant),
		tasks:    make(map[string]*task),
	}
	for _, o := range opts {
		o(s)
	}
	if s.clock == nil {
		s.clock = NewRealClock()
	}
	if s.exec == nil {
		s.exec = NewTimedExecutor(s.clock)
	}
	if s.store == nil {
		s.store = NewMemoryStore()
	}
	return s, nil
}

// Capacity returns the configured cluster capacity.
func (s *Scheduler) Capacity() Resources { return s.capacity }

// Store returns the event store.
func (s *Scheduler) Store() EventStore { return s.store }

// AddTenant registers a tenant with an explicit positive integer weight.
func (s *Scheduler) AddTenant(id string, weight int64) error {
	if strings.TrimSpace(id) == "" {
		return fmt.Errorf("%w: tenant id must be non-empty", ErrBadRequest)
	}
	if weight <= 0 {
		return fmt.Errorf("%w: weight must be positive, got %d", ErrBadRequest, weight)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.tenants[id]; ok {
		return fmt.Errorf("%w: %s", ErrTenantExists, id)
	}
	s.tenants[id] = &tenant{id: id, weight: weight}
	s.appendLocked(EventTenantCreated, id, "", nil, "")
	return nil
}

// Submit adds a task to its tenant's FIFO queue and runs a scheduling pass.
func (s *Scheduler) Submit(spec TaskSpec) error {
	if strings.TrimSpace(spec.ID) == "" {
		return fmt.Errorf("%w: task id must be non-empty", ErrBadRequest)
	}
	if strings.TrimSpace(spec.TenantID) == "" {
		return fmt.Errorf("%w: tenant_id must be non-empty", ErrBadRequest)
	}
	if spec.Request.CPU <= 0 || spec.Request.Mem <= 0 {
		return fmt.Errorf("%w: resource request must be positive in both dimensions", ErrBadRequest)
	}
	if spec.Duration.Duration <= 0 {
		return fmt.Errorf("%w: duration must be positive", ErrBadRequest)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.tenants[spec.TenantID]
	if !ok {
		return fmt.Errorf("%w: %s", ErrTenantNotFound, spec.TenantID)
	}
	if _, dup := s.tasks[spec.ID]; dup {
		return fmt.Errorf("%w: %s", ErrTaskExists, spec.ID)
	}
	s.seq++
	tk := &task{spec: spec, seq: s.seq, status: StatusWaiting}
	s.tasks[spec.ID] = tk
	t.waiting = append(t.waiting, tk)
	req := spec.Request
	s.appendLocked(EventTaskSubmitted, spec.TenantID, spec.ID, &req, "")
	// A request larger than the whole cluster can never be served.
	if !spec.Request.fitsIn(s.capacity) {
		s.appendLocked(EventTaskBlocked, spec.TenantID, spec.ID, &req,
			"request exceeds total cluster capacity; queue head blocked (other tenants unaffected)")
	}
	s.scheduleLocked()
	return nil
}

// Complete marks a running task finished (called by executors, or directly
// by tests using a manual executor) and frees its resources.
func (s *Scheduler) Complete(taskID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	tk, ok := s.tasks[taskID]
	if !ok {
		return fmt.Errorf("%w: %s", ErrTaskNotFound, taskID)
	}
	if tk.status != StatusRunning {
		return fmt.Errorf("%w: task %s is not running (status %s)", ErrBadRequest, taskID, tk.status)
	}
	t := s.tenants[tk.spec.TenantID]
	now := s.clock.Now()
	tk.status = StatusDone
	tk.finishedAt = &now
	t.finishedCount++
	t.allocated = t.allocated.sub(tk.spec.Request)
	s.used = s.used.sub(tk.spec.Request)
	// Remove from running list.
	for i, rt := range t.running {
		if rt == tk {
			t.running = append(t.running[:i], t.running[i+1:]...)
			break
		}
	}
	req := tk.spec.Request
	s.appendLocked(EventTaskFinished, t.id, tk.spec.ID, &req, "")
	s.scheduleLocked()
	return nil
}

// launchLocked starts the head task of tenant t; caller guarantees it fits.
func (s *Scheduler) launchLocked(t *tenant, tk *task) {
	now := s.clock.Now()
	tk.status = StatusRunning
	tk.startedAt = &now
	t.waiting = t.waiting[1:]
	t.running = append(t.running, tk)
	t.allocated = t.allocated.add(tk.spec.Request)
	s.used = s.used.add(tk.spec.Request)

	req := tk.spec.Request
	s.appendLocked(EventTaskStarted, t.id, tk.spec.ID, &req, "")

	id := tk.spec.ID
	spec := tk.spec
	// Executor must not invoke complete synchronously: the scheduler lock
	// is held here. TimedExecutor schedules via the clock, which is safe.
	s.exec.Run(spec, func() { _ = s.Complete(id) })
}

// scheduleLocked repeatedly starts tasks until no further start is possible.
func (s *Scheduler) scheduleLocked() {
	for {
		available := s.capacity.sub(s.used)

		// Candidates: tenants whose head task could ever fit the cluster.
		var cands []*tenant
		for _, t := range s.tenants {
			h := t.head()
			if h == nil {
				continue
			}
			if !h.spec.Request.fitsIn(s.capacity) {
				continue // permanently blocked head; tenant keeps its turn later
			}
			cands = append(cands, t)
		}
		if len(cands) == 0 {
			return
		}
		sort.SliceStable(cands, func(i, j int) bool {
			return s.tenantLess(cands[i], cands[j])
		})

		launched := false
		for _, t := range cands {
			h := t.head()
			if !h.spec.Request.fitsIn(available) {
				// Head cannot fit right now. A pass only consumes
				// resources, so it can never free up; skip this tenant
				// and keep scanning — a later tenant's smaller head may
				// still fit the current availability.
				continue
			}
			s.launchLocked(t, h)
			launched = true
			break // resources changed: restart pass and re-sort DRF order
		}
		if !launched {
			return
		}
	}
}

// tenantLess defines the global, deterministic scheduling priority:
//  1. lower weighted dominant share wins (exact rational comparison);
//  2. tie: fewer currently running tasks;
//  3. tie: lexicographically smaller tenant id.
func (s *Scheduler) tenantLess(a, b *tenant) bool {
	cmp := s.compareShare(a, b)
	if cmp != 0 {
		return cmp < 0
	}
	if len(a.running) != len(b.running) {
		return len(a.running) < len(b.running)
	}
	return a.id < b.id
}

// compareShare compares dominantShare(a)/weight(a) vs the same for b,
// returning -1/0/+1 using arbitrary-precision integer arithmetic so the
// ordering is exact regardless of unit magnitudes.
func (s *Scheduler) compareShare(a, b *tenant) int {
	cpuCap, memCap := big.NewInt(s.capacity.CPU), big.NewInt(s.capacity.Mem)
	denCommon := new(big.Int).Mul(cpuCap, memCap)

	// dom(t) = max(allocated.cpu/cpuCap, allocated.mem/memCap)
	// written over the common denominator cpuCap*memCap:
	// num(t) = max(allocated.cpu*memCap, allocated.mem*cpuCap)
	numA := maxBig(
		new(big.Int).Mul(big.NewInt(a.allocated.CPU), memCap),
		new(big.Int).Mul(big.NewInt(a.allocated.Mem), cpuCap),
	)
	numB := maxBig(
		new(big.Int).Mul(big.NewInt(b.allocated.CPU), memCap),
		new(big.Int).Mul(big.NewInt(b.allocated.Mem), cpuCap),
	)
	// full fraction denominators
	denA := new(big.Int).Mul(denCommon, big.NewInt(a.weight))
	denB := new(big.Int).Mul(denCommon, big.NewInt(b.weight))

	lhs := new(big.Int).Mul(numA, denB)
	rhs := new(big.Int).Mul(numB, denA)
	return lhs.Cmp(rhs)
}

func maxBig(a, b *big.Int) *big.Int {
	if a.Cmp(b) >= 0 {
		return a
	}
	return b
}

func (s *Scheduler) appendLocked(typ EventType, tenantID, taskID string, req *Resources, reason string) {
	avail := s.capacity.sub(s.used)
	used := s.used
	ev := Event{
		Seq:       s.seq,
		Type:      typ,
		At:        s.clock.Now(),
		TenantID:  tenantID,
		TaskID:    taskID,
		Request:   req,
		Used:      &used,
		Available: &avail,
		Reason:    reason,
	}
	s.store.Append(ev)
}

func viewOf(tk *task) TaskView {
	return TaskView{
		ID:         tk.spec.ID,
		TenantID:   tk.spec.TenantID,
		Seq:        tk.seq,
		Status:     tk.status,
		Request:    tk.spec.Request,
		Duration:   tk.spec.Duration,
		StartedAt:  tk.startedAt,
		FinishedAt: tk.finishedAt,
	}
}

func dominantShareFloat(t *tenant, cap Resources) float64 {
	cpu := 0.0
	mem := 0.0
	if cap.CPU > 0 {
		cpu = float64(t.allocated.CPU) / float64(cap.CPU)
	}
	if cap.Mem > 0 {
		mem = float64(t.allocated.Mem) / float64(cap.Mem)
	}
	if cpu > mem {
		return cpu
	}
	return mem
}

// Snapshot returns a consistent full view of the cluster and queues.
func (s *Scheduler) Snapshot() *ClusterSnapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	snap := &ClusterSnapshot{
		Capacity:  s.capacity,
		Used:      s.used,
		Available: s.capacity.sub(s.used),
		EventSeq:  int64(len(s.store.All())),
	}
	ids := make([]string, 0, len(s.tenants))
	for id := range s.tenants {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		t := s.tenants[id]
		tv := TenantView{
			ID:            t.id,
			Weight:        t.weight,
			DominantShare: dominantShareFloat(t, s.capacity),
			Allocated:     t.allocated,
			FinishedCount: t.finishedCount,
		}
		for _, tk := range t.running {
			tv.Running = append(tv.Running, viewOf(tk))
		}
		for _, tk := range t.waiting {
			tv.Waiting = append(tv.Waiting, viewOf(tk))
		}
		if h := t.head(); h != nil {
			if !h.spec.Request.fitsIn(s.capacity) {
				tv.HeadBlocked = true
				tv.HeadExceedsCap = true
			} else if !h.spec.Request.fitsIn(s.capacity.sub(s.used)) {
				tv.HeadBlocked = true
			}
		}
		snap.Tenants = append(snap.Tenants, tv)
	}
	taskIDs := make([]string, 0, len(s.tasks))
	for id := range s.tasks {
		taskIDs = append(taskIDs, id)
	}
	sort.Slice(taskIDs, func(i, j int) bool { return s.tasks[taskIDs[i]].seq < s.tasks[taskIDs[j]].seq })
	for _, id := range taskIDs {
		snap.Tasks = append(snap.Tasks, viewOf(s.tasks[id]))
	}
	return snap
}

// Events returns recent events; see EventStore.Since.
func (s *Scheduler) Events(afterID int64, limit int) []Event {
	return s.store.Since(afterID, limit)
}
