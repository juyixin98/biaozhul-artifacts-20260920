package scheduler

import (
	"context"
	"errors"
	"fmt"
	"math/big"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

// Errors returned by the scheduler API.
var (
	ErrCapacity      = errors.New("task request exceeds cluster capacity")
	ErrBadRequest    = errors.New("invalid task or tenant request")
	ErrUnknownTenant = errors.New("unknown tenant")
	ErrUnknownTask   = errors.New("unknown task")
	ErrFakeClockOnly = errors.New("endpoint supported only with a FakeClock")
	ErrClosed        = errors.New("scheduler closed")
)

// Task is a unit of work. Spec is the executor-specific command (used by
// ProcessExecutor); Duration is used by SimExecutor.
type Task struct {
	ID        string
	TenantID  string
	Req       Resources
	Duration  time.Duration
	Spec      string
	Submitted time.Time
	Started   time.Time
	Finished  time.Time
	State     TaskState
	FailMsg   string
	waitNoted bool // a TASK_WAITING event has already been emitted
	cancel    context.CancelFunc
}

// Tenant owns a weight used by weighted DRF.
type Tenant struct {
	ID     string
	Weight int64
}

// Config constructs a Scheduler.
type Config struct {
	Capacity    Resources
	Clock       Clock
	Executor    Executor
	Sink        EventSink // optional; defaults to an in-memory sink
	IDGenerator func() string
}

// Scheduler is a non-preemptive two-dimensional (CPU, memory) DRF scheduler.
type Scheduler struct {
	mu sync.Mutex

	capacity Resources
	clock    Clock
	exec     Executor
	sink     EventSink
	genID    func() string

	tenants  map[string]*Tenant
	tasks    map[string]*Task
	byTenant map[string][]*Task // FIFO queues, plus running/completed refs
	seq      int64

	used     Resources // sum over RUNNING tasks (the DRF allocation)
	runCount int
	started  bool
	closed   bool
	paused   bool // when set, the loop does not schedule (batch admission)

	kick chan struct{}
	done chan struct{}
	wg   sync.WaitGroup

	// version bumps on every observable state change (start/finish/cancel).
	// WaitQuiet uses it to detect that processing has settled.
	version atomic.Int64
}

// New constructs a scheduler. Weighted DRF: a tenant with weight w receives
// resources as if its share were share/w; higher weight => more resources.
func New(cfg Config) (*Scheduler, error) {
	if cfg.Capacity.CPU <= 0 || cfg.Capacity.Memory <= 0 {
		return nil, fmt.Errorf("%w: capacity must be positive in both dimensions, got cpu=%d mem=%d",
			ErrBadRequest, cfg.Capacity.CPU, cfg.Capacity.Memory)
	}
	if cfg.Clock == nil {
		cfg.Clock = NewRealClock()
	}
	if cfg.Executor == nil {
		cfg.Executor = NewSimExecutor(cfg.Clock)
	}
	if cfg.Sink == nil {
		cfg.Sink = NewMemorySink(0)
	}
	if cfg.IDGenerator == nil {
		var n int64
		cfg.IDGenerator = func() string {
			n++
			return fmt.Sprintf("task-%d", n)
		}
	}
	return &Scheduler{
		capacity: cfg.Capacity,
		clock:    cfg.Clock,
		exec:     cfg.Executor,
		sink:     cfg.Sink,
		genID:    cfg.IDGenerator,
		tenants:  make(map[string]*Tenant),
		tasks:    make(map[string]*Task),
		byTenant: make(map[string][]*Task),
		kick:     make(chan struct{}, 1),
		done:     make(chan struct{}),
	}, nil
}

// Start launches the scheduling goroutine. Safe to call once.
func (s *Scheduler) Start() {
	s.mu.Lock()
	if s.started {
		s.mu.Unlock()
		return
	}
	s.started = true
	s.mu.Unlock()
	s.wg.Add(1)
	go s.loop()
}

// Close stops the scheduler goroutine. Running tasks are left to the executor
// (ProcessExecutor-spawned commands keep running unless CancelTask is used).
func (s *Scheduler) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	s.mu.Unlock()
	close(s.done)
	s.wg.Wait()
	return nil
}

// AddTenant creates or updates a tenant. Weight must be >= 1.
func (s *Scheduler) AddTenant(id string, weight int64) error {
	if id == "" || weight < 1 {
		return fmt.Errorf("%w: tenant id required and weight >= 1", ErrBadRequest)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.tenants[id] = &Tenant{ID: id, Weight: weight}
	if _, ok := s.byTenant[id]; !ok {
		s.byTenant[id] = nil
	}
	s.signal()
	return nil
}

// SubmitRequest is the input for Submit.
type SubmitRequest struct {
	TenantID string
	CPU      int64
	Memory   int64
	Duration time.Duration // SimExecutor
	Spec     string        // ProcessExecutor
	ID       string        // optional explicit id
}

// Submit admits a task. A task larger than total capacity is rejected
// (it could never run); otherwise it is queued and scheduling is triggered.
// Non-preemptive: admitting never evicts a running task.
func (s *Scheduler) Submit(req SubmitRequest) (*Task, error) {
	if req.CPU <= 0 || req.Memory <= 0 {
		return nil, fmt.Errorf("%w: cpu and memory must be positive", ErrBadRequest)
	}
	if req.Duration <= 0 && req.Spec == "" {
		return nil, fmt.Errorf("%w: duration must be positive (or provide a command spec)", ErrBadRequest)
	}
	r := Resources{CPU: req.CPU, Memory: req.Memory}
	if !r.LessEqual(s.capacity) {
		return nil, fmt.Errorf("%w: request (%d cpu, %d mem) > capacity (%d cpu, %d mem)",
			ErrCapacity, r.CPU, r.Memory, s.capacity.CPU, s.capacity.Memory)
	}
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil, ErrClosed
	}
	if _, ok := s.tenants[req.TenantID]; !ok {
		s.mu.Unlock()
		return nil, fmt.Errorf("%w: %q", ErrUnknownTenant, req.TenantID)
	}
	id := req.ID
	if id == "" {
		id = s.genID()
	}
	if _, dup := s.tasks[id]; dup {
		s.mu.Unlock()
		return nil, fmt.Errorf("%w: task id %q already exists", ErrBadRequest, id)
	}
	t := &Task{
		ID:        id,
		TenantID:  req.TenantID,
		Req:       r,
		Duration:  req.Duration,
		Spec:      req.Spec,
		Submitted: s.clock.Now(),
		State:     StateQueued,
	}
	s.tasks[id] = t
	s.byTenant[req.TenantID] = append(s.byTenant[req.TenantID], t)
	s.emitLocked(EventSubmitted, t, map[string]any{
		"queue_position": len(s.byTenant[req.TenantID]),
	})
	s.mu.Unlock()
	s.signal()
	return t, nil
}

// SubmitBatch admits a group of tasks as if they all arrived at the same
// instant: scheduling is suspended while the tasks are admitted and resumed
// exactly once afterwards, so the first DRF pass sees the complete batch
// (needed to reproduce hand-computed simultaneous-arrival scenarios). It is
// atomic per request: either every task is admitted or none (on the first
// validation error).
func (s *Scheduler) SubmitBatch(reqs []SubmitRequest) ([]*Task, error) {
	if len(reqs) == 0 {
		return nil, fmt.Errorf("%w: empty batch", ErrBadRequest)
	}
	// Pre-validate everything before mutating state.
	for i, req := range reqs {
		if req.CPU <= 0 || req.Memory <= 0 {
			return nil, fmt.Errorf("%w: batch[%d]: cpu and memory must be positive", ErrBadRequest, i)
		}
		if req.Duration <= 0 && req.Spec == "" {
			return nil, fmt.Errorf("%w: batch[%d]: duration must be positive (or provide spec)", ErrBadRequest, i)
		}
		r := Resources{CPU: req.CPU, Memory: req.Memory}
		if !r.LessEqual(s.capacity) {
			return nil, fmt.Errorf("%w: batch[%d]: request %v exceeds capacity %v",
				ErrCapacity, i, r, s.capacity)
		}
	}

	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil, ErrClosed
	}
	wasPaused := s.paused
	s.paused = true

	tasks := make([]*Task, 0, len(reqs))
	rollback := func() {
		for _, t := range tasks {
			delete(s.tasks, t.ID)
			q := s.byTenant[t.TenantID]
			for j, x := range q {
				if x == t {
					s.byTenant[t.TenantID] = append(q[:j], q[j+1:]...)
					break
				}
			}
		}
		s.paused = wasPaused
		s.mu.Unlock()
	}

	for i, req := range reqs {
		if _, ok := s.tenants[req.TenantID]; !ok {
			rollback()
			return nil, fmt.Errorf("%w: batch[%d]: %q", ErrUnknownTenant, i, req.TenantID)
		}
		id := req.ID
		if id == "" {
			id = s.genID()
		}
		if _, dup := s.tasks[id]; dup {
			rollback()
			return nil, fmt.Errorf("%w: batch[%d]: task id %q already exists", ErrBadRequest, i, id)
		}
		t := &Task{
			ID:        id,
			TenantID:  req.TenantID,
			Req:       Resources{CPU: req.CPU, Memory: req.Memory},
			Duration:  req.Duration,
			Spec:      req.Spec,
			Submitted: s.clock.Now(),
			State:     StateQueued,
		}
		s.tasks[id] = t
		s.byTenant[req.TenantID] = append(s.byTenant[req.TenantID], t)
		tasks = append(tasks, t)
		s.emitLocked(EventSubmitted, t, map[string]any{"queue_position": len(s.byTenant[req.TenantID]), "batch": len(reqs)})
	}
	s.paused = wasPaused
	s.mu.Unlock()

	s.signal() // one scheduling pass observes the whole batch
	return tasks, nil
}

// CancelTask marks a queued task failed/cancelled and removes it from its
// queue; running tasks have their process context cancelled (best effort).
func (s *Scheduler) CancelTask(id, reason string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.tasks[id]
	if !ok {
		return ErrUnknownTask
	}
	switch t.State {
	case StateQueued:
		t.State = StateFailed
		t.FailMsg = "cancelled: " + reason
		t.Finished = s.clock.Now()
		q := s.byTenant[t.TenantID]
		for i, x := range q {
			if x == t {
				s.byTenant[t.TenantID] = append(q[:i], q[i+1:]...)
				break
			}
		}
		s.emitLocked(EventFinished, t, map[string]any{"status": "CANCELLED", "reason": reason})
		s.signal()
	case StateRunning:
		t.FailMsg = "cancelled: " + reason
		if t.cancel != nil {
			t.cancel()
		}
	default:
		return fmt.Errorf("%w: task already terminal", ErrBadRequest)
	}
	return nil
}

// SetCapacity resizes the cluster (e.g. node added/removed). New capacity must
// still cover every running task; scheduling is then re-evaluated.
func (s *Scheduler) SetCapacity(c Resources) error {
	if c.CPU <= 0 || c.Memory <= 0 {
		return fmt.Errorf("%w: capacity must be positive", ErrBadRequest)
	}
	s.mu.Lock()
	if c.CPU < s.used.CPU || c.Memory < s.used.Memory {
		s.mu.Unlock()
		return fmt.Errorf("%w: new capacity %v below current allocation %v (non-preemptive)",
			ErrCapacity, c, s.used)
	}
	s.capacity = c
	s.mu.Unlock()
	s.signal()
	return nil
}

// signal wakes the scheduling goroutine (non-blocking; the channel is buffered).
func (s *Scheduler) signal() {
	select {
	case s.kick <- struct{}{}:
	default:
	}
}

// Pause suppresses scheduling passes: submitted tasks stay QUEUED until
// Resume, so a batch of tasks can be admitted atomically for deterministic
// hand-computed scenarios. Completions arriving while paused are still
// finalized; they trigger a pass on Resume.
func (s *Scheduler) Pause() {
	s.mu.Lock()
	s.paused = true
	s.mu.Unlock()
}

// Resume re-enables scheduling and immediately triggers a pass.
func (s *Scheduler) Resume() {
	s.mu.Lock()
	s.paused = false
	s.mu.Unlock()
	s.signal()
}

// IsPaused reports the current pause state.
func (s *Scheduler) IsPaused() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.paused
}

// Kick is the exported form of signal (manual /v1/schedule trigger).
func (s *Scheduler) Kick() { s.signal() }

// UsedAndCounts is the allocation-free counterpart of Snapshot for hot loops:
// it returns current used resources, capacity and task counts without building
// task views or computing exact fractions.
func (s *Scheduler) UsedAndCounts() (used, capacity Resources, running, queued, completed int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	used, capacity = s.used, s.capacity
	running = s.runCount
	for _, t := range s.tasks {
		switch t.State {
		case StateQueued:
			queued++
		case StateComplete:
			completed++
		}
	}
	return used, capacity, running, queued, completed
}

// ---------------------------------------------------------------------------
// scheduling core
// ---------------------------------------------------------------------------

// pickTenant implements weighted DRF selection.
//
// For each tenant we consider its queue head only (per-tenant FIFO). Its
// weighted dominant share if started is:
//
//	max((usedCPU+reqCPU)/capCPU, (usedMem+reqMem)/capMem) / weight
//
// The tenant with the smallest resulting share wins. Fraction comparisons use
// exact integer cross-multiplication (big.Int, so no precision or overflow
// concerns). Deterministic tie-break: smallest tenant ID, then smallest queued
// task ID.
// pickTenant selects the next task to launch using (weighted) DRF.
//
// Rule (canonical DRF):
//  1. Only each tenant's FIFO queue HEAD is eligible; a head is feasible only
//     if used+request fits the cluster capacity in BOTH dimensions.
//  2. Among feasible tenants, choose the smallest CURRENT weighted dominant
//     share: max(allocCPU/capCPU, allocMem/capMem) / weight, where alloc is
//     the tenant's sum over its RUNNING tasks. Fraction comparisons are exact
//     integer arithmetic (big.Int).
//  3. Deterministic tie-break: lexicographically smaller tenant ID, then
//     smaller queued task ID.
func (s *Scheduler) pickTenant() *Task {
	// Per-tenant current allocation (sum over running tasks).
	alloc := make(map[string]Resources)
	for _, t := range s.tasks {
		if t.State == StateRunning {
			alloc[t.TenantID] = alloc[t.TenantID].Add(t.Req)
		}
	}

	var best *pickCandidate

	for _, tn := range s.sortedTenantsLocked() {
		head := s.queueHeadLocked(tn.ID)
		if head == nil {
			continue
		}
		// Feasibility is a cluster-wide, two-dimensional check against the
		// resources that launching this task would consume.
		if !s.used.Add(head.Req).LessEqual(s.capacity) {
			continue // does not fit this pass; it keeps waiting
		}

		// The tenant's CURRENT dominant share (not the projected post-launch
		// one): DRF compares where tenants are now; the request only decides
		// feasibility.
		a := alloc[tn.ID]
		c := newPickCandidate(head, tn.ID, tn.Weight, a.CPU, a.Memory,
			s.capacity.CPU, s.capacity.Memory)
		if best == nil || c.less(best) {
			best = c
		}
	}
	if best == nil {
		return nil
	}
	return best.t
}

// pickCandidate is one feasible queue head considered by pickTenant.
type pickCandidate struct {
	t      *Task
	tenant string
	weight int64
	// Dominant share on the common scale capCPU*capMem. The allocation-free
	// fast path stores it in scaled; big is set only on int64 overflow.
	scaled int64
	big    *big.Int
}

// newPickCandidate builds a candidate from the tenant's current allocation
// and the cluster capacity. The dominant share is
// max(allocCPU/capCPU, allocMem/capMem) scaled by capCPU*capMem.
func newPickCandidate(t *Task, tenant string, weight, allocCPU, allocMem, capCPU, capMem int64) *pickCandidate {
	c := &pickCandidate{t: t, tenant: tenant, weight: weight}
	// Determine the dominant dimension by cross multiplication.
	if v, ok := mul64(allocCPU, capMem); ok {
		if w, ok2 := mul64(allocMem, capCPU); ok2 && v >= w {
			c.scaled = v
			return c
		}
	}
	// Exact fallback for inputs large enough to overflow int64 products.
	dom := dominantFrac(big.NewInt(allocCPU), big.NewInt(allocMem),
		big.NewInt(capCPU), big.NewInt(capMem))
	if dom.dim == dimCPU {
		c.big = new(big.Int).Mul(dom.num, big.NewInt(capMem))
	} else {
		c.big = new(big.Int).Mul(dom.num, big.NewInt(capCPU))
	}
	return c
}

// less reports whether c should replace best: weighted dominant share
// (scaled/weight) strictly smaller, or an exact tie broken by the
// lexicographically smaller (tenantID, taskID).
func (c *pickCandidate) less(best *pickCandidate) bool {
	cmp := c.compareWeighted(best)
	if cmp != 0 {
		return cmp < 0
	}
	if c.tenant != best.tenant {
		return c.tenant < best.tenant
	}
	return c.t.ID < best.t.ID
}

// compareWeighted compares scaled/weight vs best.scaled/best.weight exactly:
// int64 cross multiplication on the allocation-free fast path, big.Int only
// when a product overflows.
func (c *pickCandidate) compareWeighted(best *pickCandidate) int {
	if c.big == nil && best.big == nil {
		l, okL := mul64(c.scaled, best.weight)
		r, okR := mul64(best.scaled, c.weight)
		if okL && okR {
			return i64cmp(l, r)
		}
	}
	lhs := new(big.Int).Mul(c.exactBig(), big.NewInt(best.weight))
	rhs := new(big.Int).Mul(best.exactBig(), big.NewInt(c.weight))
	return lhs.Cmp(rhs)
}

func i64cmp(a, b int64) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	default:
		return 0
	}
}

func (c *pickCandidate) exactBig() *big.Int {
	if c.big != nil {
		return c.big
	}
	return big.NewInt(c.scaled)
}

// mul64 returns a*b and false if the product overflows int64.
func mul64(a, b int64) (int64, bool) {
	p := a * b
	if a != 0 && p/a != b {
		return 0, false
	}
	return p, true
}

// frac is an exact fraction tagged by the resource dimension its denominator
// measures (so two fractions on different dimensions are never naively
// cross-multiplied).
type frac struct {
	num, den *big.Int
	dim      dimension
}

type dimension uint8

const (
	dimCPU dimension = iota
	dimMemory
)

// dominantFrac returns max(numCPU/denCPU, numMem/denMem) as a fraction tagged
// with the dimension on which the maximum is attained (CPU on an exact tie).
func dominantFrac(numCPU, numMem, denCPU, denMem *big.Int) frac {
	// numCPU*denMem vs numMem*denCPU
	a := new(big.Int).Mul(numCPU, denMem)
	b := new(big.Int).Mul(numMem, denCPU)
	if a.Cmp(b) >= 0 {
		return frac{num: numCPU, den: denCPU, dim: dimCPU}
	}
	return frac{num: numMem, den: denMem, dim: dimMemory}
}

func (s *Scheduler) sortedTenantsLocked() []*Tenant {
	out := make([]*Tenant, 0, len(s.tenants))
	for _, t := range s.tenants {
		out = append(out, t)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// queueHeadLocked returns the first QUEUED task of a tenant (FIFO). Completed
// and running tasks stay in byTenant for accounting but are skipped.
func (s *Scheduler) queueHeadLocked(tenant string) *Task {
	for _, t := range s.byTenant[tenant] {
		if t.State == StateQueued {
			return t
		}
	}
	return nil
}

// scheduleOnce runs one DRF pass: repeatedly launch the min-share feasible
// head task until no queued task fits. Then it annotates tasks still waiting.
func (s *Scheduler) scheduleOnce() {
	s.mu.Lock()
	defer s.mu.Unlock()

	for {
		t := s.pickTenant()
		if t == nil {
			break
		}
		s.startLocked(t)
	}
	s.noteWaitingLocked()
}

func (s *Scheduler) startLocked(t *Task) {
	if err := s.exec.Start(t, s.clock.Now()); err != nil {
		// The task never actually started; leave it QUEUED so a later pass
		// (capacity change, retry) can try again. Record the launch failure.
		s.emitLocked(EventWaiting, t, map[string]any{"reason": "EXECUTOR_REJECTED", "error": err.Error()})
		t.waitNoted = true
		return
	}
	t.State = StateRunning
	t.Started = s.clock.Now()
	s.used = s.used.Add(t.Req)
	s.runCount++
	domPct := dominantPercent(t.Req, s.capacity)
	s.emitLocked(EventStarted, t, map[string]any{
		"weight":             s.tenants[t.TenantID].Weight,
		"dominant_share_pct": domPct,
		"running_after":      s.runCount,
		"used_after":         s.used,
	})
}

// noteWaitingLocked emits one TASK_WAITING per still-queued task whenever it
// has not been noted since its last queue transition (submit). The reason is
// computed from current state.
func (s *Scheduler) noteWaitingLocked() {
	for _, tn := range s.sortedTenantsLocked() {
		for _, t := range s.byTenant[tn.ID] {
			if t.State != StateQueued {
				continue
			}
			if !t.Req.LessEqual(s.capacity) {
				// Cannot happen post-validation unless capacity shrank below a
				// queued requirement; keep the defensive branch.
				s.noteOneLocked(t, "OVER_CAPACITY")
				continue
			}
			if head := s.queueHeadLocked(tn.ID); head == t {
				// Head fits capacity globally but didn't fit free resources in
				// any pass: resources are occupied by non-preemptible tasks.
				s.noteOneLocked(t, "INSUFFICIENT_RESOURCES")
			} else {
				// Behind another task of the same tenant (per-tenant FIFO).
				s.noteOneLocked(t, "TENANT_QUEUE_FULL")
			}
		}
	}
}

func (s *Scheduler) noteOneLocked(t *Task, reason string) {
	if t.waitNoted {
		return
	}
	t.waitNoted = true
	s.emitLocked(EventWaiting, t, map[string]any{"reason": reason})
}

func (s *Scheduler) emitLocked(typ EventType, t *Task, detail map[string]any) {
	s.seq++
	ev := Event{
		Seq:       s.seq,
		Time:      s.clock.Now(),
		Type:      typ,
		TenantID:  t.TenantID,
		TaskID:    t.ID,
		Resources: t.Req,
		Detail:    detail,
	}
	s.sink.Record(ev)
	if typ == EventStarted || typ == EventFinished {
		s.version.Add(1)
	}
}

// dominantPercent returns max(reqCPU/capCPU, reqMem/capMem) as an integer
// percentage 0..100 (rounded down), for human-readable event detail.
func dominantPercent(req, cap Resources) int {
	c := req.CPU * 100 / cap.CPU
	m := req.Memory * 100 / cap.Memory
	if c > m {
		return int(c)
	}
	return int(m)
}

// ---------------------------------------------------------------------------
// main loop
// ---------------------------------------------------------------------------

// RunOnePassSync is a synchronous driver intended for deterministic tests that
// do not Start the scheduler goroutine: it finalizes every executor
// completion already available (in deterministic task-id order), then runs a
// single DRF scheduling pass. It returns the number of completions consumed.
//
// Used together with FakeClock.Advance (which delivers completion callbacks
// synchronously) this makes a scenario fully deterministic regardless of host
// scheduling speed.
func (s *Scheduler) RunOnePassSync() int {
	var batch []ExecResult
	for {
		select {
		case r := <-s.exec.Results():
			batch = append(batch, r)
		default:
			if len(batch) > 0 {
				s.handleCompletions(batch)
			}
			s.scheduleOnce()
			return len(batch)
		}
	}
}

func (s *Scheduler) loop() {
	defer s.wg.Done()
	completions := s.exec.Results()
	for {
		s.mu.Lock()
		paused := s.paused
		s.mu.Unlock()
		if !paused {
			s.scheduleOnce()
		}
		select {
		case <-s.done:
			return
		case <-s.kick:
		case r := <-completions:
			s.handleCompletions([]ExecResult{r})
		}
	}
}

// handleCompletions finalizes a batch of finished tasks (all completion
// channels currently ready) and then lets the next iteration reschedule.
func (s *Scheduler) handleCompletions(first []ExecResult) {
	s.mu.Lock()
	batch := first
	// Drain any other completions already queued, to free all resources
	// together before the next DRF pass.
drain:
	for {
		select {
		case r := <-s.exec.Results():
			batch = append(batch, r)
		default:
			break drain
		}
	}
	// Deterministic ordering for equal completion times.
	sort.Slice(batch, func(i, j int) bool { return batch[i].TaskID < batch[j].TaskID })
	for _, r := range batch {
		t := s.tasks[r.TaskID]
		if t == nil || t.State != StateRunning {
			continue
		}
		t.Finished = s.clock.Now()
		status := "SUCCESS"
		if r.Err != nil {
			t.State = StateFailed
			t.FailMsg = r.Err.Error()
			status = "FAILED"
		} else {
			t.State = StateComplete
		}
		s.used = s.used.Sub(t.Req)
		s.runCount--
		s.emitLocked(EventFinished, t, map[string]any{
			"status":     status,
			"used_after": s.used,
			"ran_for":    t.Finished.Sub(t.Started).String(),
		})
	}
	s.mu.Unlock()
}

// WaitIdle blocks until no task is running or queued, the executor result
// channel is drained, and the clock (if fake) has no pending timer that could
// change state. Polls because the design is goroutine-driven; timeout gives
// tests a hard failure rather than hanging.
func (s *Scheduler) WaitIdle(timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		s.mu.Lock()
		idle := s.runCount == 0
		for _, t := range s.tasks {
			if t.State == StateQueued || t.State == StateRunning {
				idle = false
			}
		}
		s.mu.Unlock()
		if idle {
			return nil
		}
		time.Sleep(2 * time.Millisecond)
	}
	return errors.New("WaitIdle timed out")
}

// WaitQuiet blocks until the state version has stayed unchanged for stableFor
// (i.e. the scheduler has processed everything currently in flight and no
// further start/finish occurs). Unlike WaitIdle, running tasks may remain.
// Used after FakeClock.Advance so HTTP responses reflect post-advance state.
func (s *Scheduler) WaitQuiet(timeout, stableFor time.Duration) error {
	deadline := time.Now().Add(timeout)
	last := s.version.Load()
	lastChange := time.Now()
	for time.Now().Before(deadline) {
		time.Sleep(2 * time.Millisecond)
		v := s.version.Load()
		if v != last {
			last = v
			lastChange = time.Now()
			continue
		}
		if time.Since(lastChange) >= stableFor {
			return nil
		}
	}
	return errors.New("WaitQuiet timed out")
}

// WaitIdleOrQuiet returns when fully idle, or when state has been stable for
// 20ms, whichever comes first. Bounded by timeout.
func (s *Scheduler) WaitIdleOrQuiet(timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if err := s.WaitQuiet(25*time.Millisecond, 20*time.Millisecond); err == nil {
			return nil
		}
	}
	return errors.New("WaitIdleOrQuiet timed out")
}
