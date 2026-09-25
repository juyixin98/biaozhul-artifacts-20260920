package scheduler

import (
	"math/big"
	"sort"
	"time"
)

// TaskView is the JSON-safe representation of a task.
type TaskView struct {
	ID          string     `json:"id"`
	TenantID    string     `json:"tenant_id"`
	CPU         int64      `json:"cpu_millicpu"`
	Memory      int64      `json:"memory_mib"`
	State       TaskState  `json:"state"`
	SubmittedAt time.Time  `json:"submitted_at"`
	StartedAt   *time.Time `json:"started_at,omitempty"`
	FinishedAt  *time.Time `json:"finished_at,omitempty"`
	DurationMS  int64      `json:"duration_ms,omitempty"`
	Spec        string     `json:"spec,omitempty"`
	FailMsg     string     `json:"fail_msg,omitempty"`
}

// TenantView describes a tenant, its queue and exact DRF share.
type TenantView struct {
	ID               string    `json:"id"`
	Weight           int64     `json:"weight"`
	Running          []string  `json:"running_task_ids"`
	Queued           []string  `json:"queued_task_ids"`
	Allocated        Resources `json:"allocated"`
	DominantShare    string    `json:"dominant_share"`     // exact fraction, e.g. "2/5"
	DominantSharePct float64   `json:"dominant_share_pct"` // decimal, 4 places
	WeightedShare    string    `json:"weighted_share"`     // dominant/weight, exact
	WeightedSharePct float64   `json:"weighted_share_pct"`
}

// Snapshot is the full readable scheduler state.
type Snapshot struct {
	Now       time.Time        `json:"now"`
	Capacity  Resources        `json:"capacity"`
	Used      Resources        `json:"used"`
	Free      Resources        `json:"free"`
	Running   int              `json:"running_tasks"`
	Queued    int              `json:"queued_tasks"`
	Completed int              `json:"completed_tasks"`
	Failed    int              `json:"failed_tasks"`
	Tenants   []TenantView     `json:"tenants"`
	Tasks     []TaskView       `json:"tasks"`
	Invariant *InvariantReport `json:"invariant_check,omitempty"`
}

// InvariantReport reports the no-overcommit invariant numerically.
type InvariantReport struct {
	UsedFitsCapacity bool  `json:"used_fits_capacity"`
	CPUUsed          int64 `json:"cpu_used"`
	CPUCapacity      int64 `json:"cpu_capacity"`
	MemUsed          int64 `json:"mem_used"`
	MemCapacity      int64 `json:"mem_capacity"`
}

// Snapshot returns a consistent point-in-time view.
func (s *Scheduler) Snapshot() Snapshot {
	s.mu.Lock()
	defer s.mu.Unlock()

	snap := Snapshot{
		Now:      s.clock.Now(),
		Capacity: s.capacity,
		Used:     s.used,
		Free:     s.capacity.Sub(s.used),
		Running:  s.runCount,
	}
	tenantAlloc := make(map[string]Resources)
	for _, t := range s.tasks {
		view := taskView(t)
		switch t.State {
		case StateQueued:
			snap.Queued++
		case StateRunning:
			tenantAlloc[t.TenantID] = tenantAlloc[t.TenantID].Add(t.Req)
		case StateComplete:
			snap.Completed++
		case StateFailed:
			snap.Failed++
		}
		snap.Tasks = append(snap.Tasks, view)
	}
	sort.Slice(snap.Tasks, func(i, j int) bool { return snap.Tasks[i].ID < snap.Tasks[j].ID })

	for _, tn := range s.sortedTenantsLocked() {
		tv := TenantView{
			ID:        tn.ID,
			Weight:    tn.Weight,
			Allocated: tenantAlloc[tn.ID],
		}
		for _, t := range s.byTenant[tn.ID] {
			switch t.State {
			case StateRunning:
				tv.Running = append(tv.Running, t.ID)
			case StateQueued:
				tv.Queued = append(tv.Queued, t.ID)
			}
		}
		alloc := tenantAlloc[tn.ID]
		reduced, _, domPct := dominantFraction(alloc, s.capacity)
		tv.DominantShare = reduced
		tv.DominantSharePct = domPct
		wNum := bigFractionDiv(reduced, tn.Weight) // dominant/weight, exact
		tv.WeightedShare = wNum
		tv.WeightedSharePct = fracPercent(wNum)
		snap.Tenants = append(snap.Tenants, tv)
	}
	snap.Invariant = &InvariantReport{
		UsedFitsCapacity: s.used.LessEqual(s.capacity),
		CPUUsed:          s.used.CPU,
		CPUCapacity:      s.capacity.CPU,
		MemUsed:          s.used.Memory,
		MemCapacity:      s.capacity.Memory,
	}
	return snap
}

// GetTask returns a single task view.
func (s *Scheduler) GetTask(id string) (TaskView, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.tasks[id]
	if !ok {
		return TaskView{}, ErrUnknownTask
	}
	return taskView(t), nil
}

// Events returns retained events (MemorySink only).
func (s *Scheduler) Events(limit int) []Event {
	ms, ok := s.sink.(*MemorySink)
	if !ok {
		return nil
	}
	evs := ms.Events()
	if limit > 0 && len(evs) > limit {
		evs = evs[len(evs)-limit:]
	}
	return evs
}

func taskView(t *Task) TaskView {
	v := TaskView{
		ID:          t.ID,
		TenantID:    t.TenantID,
		CPU:         t.Req.CPU,
		Memory:      t.Req.Memory,
		State:       t.State,
		SubmittedAt: t.Submitted,
		DurationMS:  t.Duration.Milliseconds(),
		Spec:        t.Spec,
		FailMsg:     t.FailMsg,
	}
	if !t.Started.IsZero() {
		st := t.Started
		v.StartedAt = &st
	}
	if !t.Finished.IsZero() {
		ft := t.Finished
		v.FinishedAt = &ft
	}
	return v
}

// dominantFraction returns max(cpu/capCPU, mem/capMem) as a reduced "n/d"
// string plus a decimal percentage (4 fractional digits, rounded).
func dominantFraction(alloc, cap Resources) (string, string, float64) {
	nCPU, dCPU := big.NewInt(alloc.CPU), big.NewInt(cap.CPU)
	nMem, dMem := big.NewInt(alloc.Memory), big.NewInt(cap.Memory)
	dom := dominantFrac(nCPU, nMem, dCPU, dMem)
	reduced := reduceString(dom.num, dom.den)
	return reduced, dom.num.String() + "/" + dom.den.String(), fracPercent(reduced)
}

func reduceString(n, d *big.Int) string {
	n2, d2 := new(big.Int), new(big.Int)
	g := new(big.Int).GCD(nil, nil, n, d)
	n2.Quo(n, g)
	d2.Quo(d, g)
	return n2.String() + "/" + d2.String()
}

// bigFractionDiv takes "n/d" and divides by w, returning reduced "n/(d*w)".
func bigFractionDiv(fracStr string, w int64) string {
	n, d, ok := parseFrac(fracStr)
	if !ok {
		return fracStr
	}
	d.Mul(d, big.NewInt(w))
	return reduceString(n, d)
}

func parseFrac(s string) (*big.Int, *big.Int, bool) {
	for i := 0; i < len(s); i++ {
		if s[i] == '/' {
			n, ok := new(big.Int).SetString(s[:i], 10)
			if !ok {
				return nil, nil, false
			}
			d, ok := new(big.Int).SetString(s[i+1:], 10)
			return n, d, ok
		}
	}
	return nil, nil, false
}

// fracPercent converts "n/d" to 100*n/d rounded to 4 decimal places.
func fracPercent(fracStr string) float64 {
	n, d, ok := parseFrac(fracStr)
	if !ok {
		return 0
	}
	f := new(big.Float).Quo(
		new(big.Float).SetInt(n),
		new(big.Float).SetInt(d),
	)
	pct, _ := new(big.Float).Mul(f, big.NewFloat(100)).Float64()
	return round4(pct)
}

func round4(f float64) float64 {
	return float64(int64(f*10000+0.5)) / 10000
}
