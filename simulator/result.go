package simulator

// TaskResult is the per-task outcome extracted from the runtime trace.
type TaskResult struct {
	Name           string        `json:"name"`
	BasePriority   int           `json:"basePriority"`
	ReleaseTime    int           `json:"releaseTime"`
	StartTime      *int          `json:"startTime,omitempty"`
	FinishTime     *int          `json:"finishTime,omitempty"`
	Completed      bool          `json:"completed"`
	RunningTicks   int           `json:"runningTicks"`
	BlockedTicks   int           `json:"blockedTicks"`
	LockAttempts   []LockAttempt `json:"lockAttempts"`
	TotalLockWaits int           `json:"totalLockWaits"`
}

// Result is one full simulation run.
type Result struct {
	EnableInheritance bool         `json:"enableInheritance"`
	Events            []Event      `json:"events"`
	Tasks             []TaskResult `json:"tasks"`
	Makespan          int          `json:"makespan"` // completion time of the last task
	Completed         bool         `json:"completed"`
	Deadlocked        bool         `json:"deadlocked"`
	TimedOut          bool         `json:"timedOut"`
	Status            string       `json:"status"` // "completed" | "deadlocked" | "timeout"
	TotalBlockedTicks int          `json:"totalBlockedTicks"`
	EventCount        int          `json:"eventCount"`
}

// TaskDelta compares one task between the two runs.
type TaskDelta struct {
	Name               string `json:"name"`
	BasePriority       int    `json:"basePriority"`
	BlockedWithInherit int    `json:"blockedWithInheritance"`
	BlockedWithout     int    `json:"blockedWithoutInheritance"`
	BlockingReduced    int    `json:"blockingReducedTicks"` // without - with
	FinishWithInherit  *int   `json:"finishWithInheritance,omitempty"`
	FinishWithout      *int   `json:"finishWithoutInheritance,omitempty"`
}

// CompareResult holds both runs and their difference.
type CompareResult struct {
	WithInheritance    Result      `json:"withInheritance"`
	WithoutInheritance Result      `json:"withoutInheritance"`
	Deltas             []TaskDelta `json:"deltas"`
	// HighPriorityWorstBlock focuses on the classic acceptance metric:
	// blocking suffered by the highest-base-priority task that ever waits.
	HighestWaiter               string `json:"highestPriorityWaitingTask"`
	HighestWaiterBlockedWith    int    `json:"highestWaiterBlockedWithInheritance"`
	HighestWaiterBlockedWithout int    `json:"highestWaiterBlockedWithoutInheritance"`
}

func (e *engine) buildResult() Result {
	res := Result{
		EnableInheritance: e.cfg.EnableInheritance,
		Events:            e.events,
		EventCount:        len(e.events),
		Deadlocked:        e.deadlocked,
		TimedOut:          e.timedOut,
	}
	makespan := 0
	completed := true
	for _, t := range e.tasks {
		tr := TaskResult{
			Name:         t.spec.Name,
			BasePriority: t.spec.Priority,
			ReleaseTime:  t.spec.ReleaseTime,
			StartTime:    t.startTime,
			FinishTime:   t.finishTime,
			Completed:    t.state == stateDone,
			RunningTicks: t.runningTicks,
			BlockedTicks: t.blockedTicks,
			LockAttempts: append([]LockAttempt(nil), t.attempts...),
		}
		for _, a := range tr.LockAttempts {
			tr.TotalLockWaits += a.WaitedTicks
		}
		res.Tasks = append(res.Tasks, tr)
		res.TotalBlockedTicks += t.blockedTicks
		if t.finishTime != nil && *t.finishTime > makespan {
			makespan = *t.finishTime
		}
		if t.state != stateDone {
			completed = false
		}
	}
	res.Makespan = makespan
	res.Completed = completed && !e.deadlocked && !e.timedOut
	switch {
	case e.deadlocked:
		res.Status = "deadlocked"
	case e.timedOut:
		res.Status = "timeout"
	default:
		res.Status = "completed"
	}
	return res
}

// Compare runs cfg workloads with and without inheritance and joins them.
func Compare(req CompareRequest) (CompareResult, error) {
	with, err := Run(Config{EnableInheritance: true, Tasks: req.Tasks, MaxTime: req.MaxTime})
	if err != nil {
		return CompareResult{}, err
	}
	without, err := Run(Config{EnableInheritance: false, Tasks: req.Tasks, MaxTime: req.MaxTime})
	if err != nil {
		return CompareResult{}, err
	}
	out := CompareResult{WithInheritance: with, WithoutInheritance: without}
	wm := map[string]TaskResult{}
	for _, tr := range with.Tasks {
		wm[tr.Name] = tr
	}
	for _, wo := range without.Tasks {
		wi := wm[wo.Name]
		out.Deltas = append(out.Deltas, TaskDelta{
			Name:               wo.Name,
			BasePriority:       wo.BasePriority,
			BlockedWithInherit: wi.BlockedTicks,
			BlockedWithout:     wo.BlockedTicks,
			BlockingReduced:    wo.BlockedTicks - wi.BlockedTicks,
			FinishWithInherit:  wi.FinishTime,
			FinishWithout:      wo.FinishTime,
		})
	}
	// Highest-base-priority task that attempted a contested lock in either run.
	name := ""
	prio := -1
	for _, d := range out.Deltas {
		if (d.BlockedWithInherit > 0 || d.BlockedWithout > 0) && d.BasePriority > prio {
			prio = d.BasePriority
			name = d.Name
		}
	}
	out.HighestWaiter = name
	for _, d := range out.Deltas {
		if d.Name == name {
			out.HighestWaiterBlockedWith = d.BlockedWithInherit
			out.HighestWaiterBlockedWithout = d.BlockedWithout
		}
	}
	return out, nil
}
