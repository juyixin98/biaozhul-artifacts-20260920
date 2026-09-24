// Package scheduler implements multi-resource Dominant Resource Fairness (DRF)
// scheduling for indivisible (non-preemptible) tasks across CPU and memory.
//
// Each tenant has:
//   - a positive integer weight (share multiplier);
//   - an optional quota: an absolute cap on simultaneously-used resources.
//
// In each scheduling round the scheduler picks the eligible tenant whose
// weighted dominant share is smallest, places that tenant's head-of-queue
// task if it physically fits, and repeats until no further task can be
// placed. Resources are never over-committed: tasks that do not fit remain
// queued and are reconsidered on every later schedule pass (e.g. after a
// release).
package scheduler

import (
	"errors"
	"math/big"
	"sort"
	"sync"
)

var (
	ErrNotConfigured   = errors.New("cluster capacity not configured")
	ErrAlreadyConfig   = errors.New("cluster already configured with active tasks; reset before reconfiguring")
	ErrInvalidCapacity = errors.New("capacity cpu and memory must both be positive")
	ErrTenantExists    = errors.New("tenant already exists")
	ErrTenantNotFound  = errors.New("tenant not found")
	ErrTaskExists      = errors.New("task already exists")
	ErrTaskNotFound    = errors.New("task not found")
	ErrBadRequest      = errors.New("invalid request")
)

// Resources is an amount of CPU (millicores) and memory (MiB), both integers.
type Resources struct {
	CPU int64 `json:"cpu"`
	Mem int64 `json:"mem"`
}

func (r Resources) add(o Resources) Resources { return Resources{r.CPU + o.CPU, r.Mem + o.Mem} }
func (r Resources) sub(o Resources) Resources { return Resources{r.CPU - o.CPU, r.Mem - o.Mem} }

// within reports whether r fits inside cap on every resource dimension.
func (r Resources) within(cap Resources) bool { return r.CPU <= cap.CPU && r.Mem <= cap.Mem }

func (r Resources) nonNegative() bool { return r.CPU >= 0 && r.Mem >= 0 }

// Task is an indivisible unit of work with a fixed two-resource demand.
type Task struct {
	ID       string    `json:"id"`
	Tenant   string    `json:"tenant"`
	Demand   Resources `json:"demand"`
	Seq      int64     `json:"seq"` // global submission order, ascending
	Released bool      `json:"-"`
}

// Tenant holds configuration and per-tenant state.
type Tenant struct {
	Name   string    `json:"name"`
	Weight int64     `json:"weight"`
	Quota  Resources // zero component => no cap on that dimension
	used   Resources
	queue  []*Task // pending tasks, FIFO by submission order
}

// effectiveQuota converts the "zero means unlimited" quota into the concrete
// cap used for placement checks (unlimited => cluster capacity).
func (t *Tenant) effectiveQuota(capacity Resources) Resources {
	q := t.Quota
	if q.CPU <= 0 {
		q.CPU = capacity.CPU
	}
	if q.Mem <= 0 {
		q.Mem = capacity.Mem
	}
	return q
}

// Scheduler is the concurrency-safe DRF scheduler state.
type Scheduler struct {
	mu         sync.Mutex
	capacity   Resources
	configured bool
	tenants    map[string]*Tenant
	tasks      map[string]*Task
	used       Resources // total allocated resources; conservation invariant:
	//   used == sum over running tasks of demand, and used within capacity.
	active  map[string]*Task // running (placed, not yet released) tasks
	nextSeq int64
}

// New creates an empty scheduler. Call Configure before use.
func New() *Scheduler {
	return &Scheduler{
		tenants: map[string]*Tenant{},
		tasks:   map[string]*Task{},
		active:  map[string]*Task{},
	}
}

// Configure sets total cluster capacity. It is only accepted when no tasks
// are active, so the running allocation can never exceed the new capacity.
func (s *Scheduler) Configure(cap Resources) error {
	if cap.CPU <= 0 || cap.Mem <= 0 {
		return ErrInvalidCapacity
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.configured && (len(s.active) > 0 || !s.used.nonNegative()) {
		return ErrAlreadyConfig
	}
	s.capacity = cap
	s.configured = true
	return nil
}

// Reset clears all tenants, tasks and capacity. Intended for tests/demos.
func (s *Scheduler) Reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.capacity = Resources{}
	s.configured = false
	s.tenants = map[string]*Tenant{}
	s.tasks = map[string]*Task{}
	s.active = map[string]*Task{}
	s.used = Resources{}
	s.nextSeq = 0
}

// AddTenant registers a tenant. weight must be positive; a zero quota
// component means "no cap on that resource dimension".
func (s *Scheduler) AddTenant(name string, weight int64, quota Resources) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if name == "" {
		return errors.Join(ErrBadRequest, errors.New("tenant name required"))
	}
	if weight <= 0 {
		return errors.Join(ErrBadRequest, errors.New("weight must be positive"))
	}
	if _, ok := s.tenants[name]; ok {
		return ErrTenantExists
	}
	s.tenants[name] = &Tenant{Name: name, Weight: weight, Quota: quota}
	return nil
}

// Submit enqueues a task for a tenant and immediately runs a scheduling pass.
// Tasks whose demand exceeds the cluster capacity or the tenant's quota can
// never be placed and are rejected up front (400); tasks that merely do not
// fit right now are kept in the tenant's queue.
func (s *Scheduler) Submit(id, tenantName string, demand Resources) (*Task, error) {
	if id == "" {
		return nil, errors.Join(ErrBadRequest, errors.New("task id required"))
	}
	if demand.CPU < 0 || demand.Mem < 0 {
		return nil, errors.Join(ErrBadRequest, errors.New("demand must be non-negative"))
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.configured {
		return nil, ErrNotConfigured
	}
	if _, ok := s.tasks[id]; ok {
		return nil, ErrTaskExists
	}
	t, ok := s.tenants[tenantName]
	if !ok {
		return nil, ErrTenantNotFound
	}
	if !demand.within(s.capacity) {
		return nil, errors.Join(ErrBadRequest, errors.New("task demand exceeds cluster capacity; it can never be placed"))
	}
	if !demand.within(t.effectiveQuota(s.capacity)) {
		return nil, errors.Join(ErrBadRequest, errors.New("task demand exceeds tenant quota; it can never be placed"))
	}
	s.nextSeq++
	task := &Task{ID: id, Tenant: tenantName, Demand: demand, Seq: s.nextSeq}
	s.tasks[id] = task
	t.queue = append(t.queue, task)
	s.scheduleLocked()
	return task, nil
}

// Release ends a running task, frees its resources and reruns scheduling so
// queued tasks can take the freed capacity. Releasing an unknown,
// already-released, or still-queued task is an error (queued tasks hold no
// resources, so there is nothing to release).
func (s *Scheduler) Release(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	task, ok := s.tasks[id]
	if !ok {
		return ErrTaskNotFound
	}
	if task.Released {
		return errors.Join(ErrBadRequest, errors.New("task already released"))
	}
	if _, running := s.active[id]; !running {
		return errors.Join(ErrBadRequest, errors.New("task is queued, not running; nothing to release"))
	}
	t, ok := s.tenants[task.Tenant]
	if !ok {
		return ErrTenantNotFound
	}
	task.Released = true
	delete(s.active, id)
	t.used = t.used.sub(task.Demand)
	s.used = s.used.sub(task.Demand)
	s.scheduleLocked()
	return nil
}

// Schedule explicitly triggers a scheduling pass.
func (s *Scheduler) Schedule() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.scheduleLocked()
}

// scheduleLocked is the core DRF loop. Caller must hold s.mu.
//
// Each round:
//  1. For every tenant with a non-empty queue, look at its head task
//     (per-tenant FIFO — a blocked head blocks the tail on purpose).
//  2. A task is eligible if it fits in the cluster's free resources and in
//     the tenant's remaining quota.
//  3. Among tenants with an eligible head, choose the one with the smallest
//     weighted dominant share (dominant use / (capacity * weight)). Ties are
//     broken by tenant id lexicographically — the deterministic tie rule.
//  4. Place the task, update usage, and repeat. A round in which no tenant is
//     eligible ends the pass.
func (s *Scheduler) scheduleLocked() {
	if !s.configured {
		return
	}
	for {
		var best *Tenant
		for _, t := range s.tenants {
			if len(t.queue) == 0 {
				continue
			}
			head := t.queue[0]
			free := s.capacity.sub(s.used)
			remainingQuota := t.effectiveQuota(s.capacity).sub(t.used)
			if !head.Demand.within(free) || !head.Demand.within(remainingQuota) {
				continue
			}
			if best == nil || lessWeightedShare(t, best, s.capacity) {
				best = t
			}
		}
		if best == nil {
			return
		}
		task := best.queue[0]
		best.queue = best.queue[1:]
		best.used = best.used.add(task.Demand)
		s.used = s.used.add(task.Demand)
		s.active[task.ID] = task
	}
}

// dominantFraction returns the dominant resource use ratio as a fraction
// num/den: max(used.cpu/cap.cpu, used.mem/cap.mem).
func dominantFraction(used, cap Resources) (num, den int64) {
	// Compare cpu*cap.mem vs mem*cap.cpu to pick the dominant dimension.
	if used.CPU*cap.Mem >= used.Mem*cap.CPU {
		return used.CPU, cap.CPU
	}
	return used.Mem, cap.Mem
}

// lessWeightedShare reports whether tenant a's weighted dominant share is
// smaller than b's. Weighted share is num/(den*weight). The comparison uses
// exact big.Int cross multiplication so results are deterministic regardless
// of floating-point rounding or input magnitudes.
func lessWeightedShare(a, b *Tenant, cap Resources) bool {
	na, da := dominantFraction(a.used, cap)
	nb, db := dominantFraction(b.used, cap)
	// a < b  <=>  na/(da*wa) < nb/(db*wb)
	//         <=>  na*db*wb < nb*da*wa
	lhs := new(big.Int).SetInt64(na)
	lhs.Mul(lhs, big.NewInt(db))
	lhs.Mul(lhs, big.NewInt(b.Weight))
	rhs := new(big.Int).SetInt64(nb)
	rhs.Mul(rhs, big.NewInt(da))
	rhs.Mul(rhs, big.NewInt(a.Weight))
	if c := lhs.Cmp(rhs); c != 0 {
		return c < 0
	}
	// Deterministic tie-break: lexicographically smaller tenant id first.
	return a.Name < b.Name
}

// ---------- read-only snapshots ----------

// TaskView is the external representation of a task.
type TaskView struct {
	ID     string    `json:"id"`
	Tenant string    `json:"tenant"`
	Demand Resources `json:"demand"`
	Seq    int64     `json:"seq"`
	State  string    `json:"state"` // "running" or "queued"
}

// TenantView is the external representation of tenant state.
type TenantView struct {
	Name          string    `json:"name"`
	Weight        int64     `json:"weight"`
	Quota         Resources `json:"quota"`
	Used          Resources `json:"used"`
	DominantShare float64   `json:"dominantShare"` // unweighted
	WeightedShare float64   `json:"weightedDominantShare"`
	RunningCount  int       `json:"runningCount"`
	QueuedCount   int       `json:"queuedCount"`
	Queued        []string  `json:"queuedTaskIds"`
}

// Snapshot is the full scheduler state returned by GET /state.
type Snapshot struct {
	Configured bool         `json:"configured"`
	Capacity   Resources    `json:"capacity"`
	Used       Resources    `json:"used"`
	Free       Resources    `json:"free"`
	Running    []TaskView   `json:"running"`
	Queued     []TaskView   `json:"queued"`
	Tenants    []TenantView `json:"tenants"`
}

// Snapshot returns a point-in-time, deterministically ordered copy of state.
func (s *Scheduler) Snapshot() Snapshot {
	s.mu.Lock()
	defer s.mu.Unlock()

	snap := Snapshot{Configured: s.configured}
	snap.Running = []TaskView{}
	snap.Queued = []TaskView{}
	snap.Tenants = []TenantView{}
	if !s.configured {
		return snap
	}
	snap.Capacity = s.capacity
	snap.Used = s.used
	snap.Free = s.capacity.sub(s.used)

	for _, task := range s.active {
		snap.Running = append(snap.Running, TaskView{
			ID: task.ID, Tenant: task.Tenant, Demand: task.Demand, Seq: task.Seq, State: "running",
		})
	}
	sort.Slice(snap.Running, func(i, j int) bool { return snap.Running[i].Seq < snap.Running[j].Seq })

	for _, t := range s.tenants {
		num, den := dominantFraction(t.used, s.capacity)
		domShare := float64(num) / float64(den)
		running := 0
		for _, task := range s.active {
			if task.Tenant == t.Name {
				running++
			}
		}
		view := TenantView{
			Name: t.Name, Weight: t.Weight, Quota: t.Quota, Used: t.used,
			DominantShare: domShare, WeightedShare: domShare / float64(t.Weight),
			RunningCount: running, Queued: []string{},
		}
		for _, qt := range t.queue {
			view.Queued = append(view.Queued, qt.ID)
			snap.Queued = append(snap.Queued, TaskView{
				ID: qt.ID, Tenant: qt.Tenant, Demand: qt.Demand, Seq: qt.Seq, State: "queued",
			})
		}
		view.QueuedCount = len(t.queue)
		snap.Tenants = append(snap.Tenants, view)
	}
	sort.Slice(snap.Queued, func(i, j int) bool { return snap.Queued[i].Seq < snap.Queued[j].Seq })
	sort.Slice(snap.Tenants, func(i, j int) bool { return snap.Tenants[i].Name < snap.Tenants[j].Name })
	return snap
}
