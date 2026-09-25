package scenario

import "pilab/scheduler"

// TaskMetric holds per-task measurements used to quantify priority inversion.
type TaskMetric struct {
	Task           string `json:"task"`
	BasePriority   int    `json:"basePriority"`
	FinishTick     int64  `json:"finishTick"`
	BlockedTicks   int64  `json:"blockedTicks"`
	PriorityBoosts int    `json:"priorityBoosts"`
}

// Comparison runs the inversion workload both with and without PIP and reports
// side-by-side metrics, demonstrating the inversion and its mitigation.
type Comparison struct {
	NonePIP      *scheduler.Report `json:"withoutInheritance"`
	PIP          *scheduler.Report `json:"withInheritance"`
	HighTask     string            `json:"highPriorityTask"`
	NoneMetric   TaskMetric        `json:"withoutInheritanceMetric"`
	PIPMetric    TaskMetric        `json:"pipMetric"`
	FinishDelta  int64             `json:"highFinishTickSaved"`
	BlockedDelta int64             `json:"highBlockedTicksSaved"`
}

// CompareInversion executes both inversion variants and summarizes them.
func CompareInversion() (*Comparison, error) {
	scNone, _ := Get(InversionNone)
	scPIP, _ := Get(InversionPIP)
	rNone, err := Run(scNone)
	if err != nil {
		return nil, err
	}
	rPIP, err := Run(scPIP)
	if err != nil {
		return nil, err
	}
	const H = "H"
	c := &Comparison{
		NonePIP:  rNone,
		PIP:      rPIP,
		HighTask: H,
	}
	c.NoneMetric = metric(rNone, H)
	c.PIPMetric = metric(rPIP, H)
	c.FinishDelta = c.NoneMetric.FinishTick - c.PIPMetric.FinishTick
	c.BlockedDelta = c.NoneMetric.BlockedTicks - c.PIPMetric.BlockedTicks
	return c, nil
}

func metric(r *scheduler.Report, id string) TaskMetric {
	for _, t := range r.Tasks {
		if t.ID == id {
			return TaskMetric{
				Task:           t.ID,
				BasePriority:   t.BasePriority,
				FinishTick:     t.FinishTick,
				BlockedTicks:   t.BlockedTicks,
				PriorityBoosts: t.PriorityBoosts,
			}
		}
	}
	return TaskMetric{Task: id}
}
