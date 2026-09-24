package sim

import "fmt"

// Validate checks a workload for static errors before simulation.
func Validate(w Workload) error {
	seenRes := map[string]bool{}
	for _, r := range w.Resources {
		if r == "" {
			return fmt.Errorf("resources must not contain empty names")
		}
		if seenRes[r] {
			return fmt.Errorf("duplicate resource %q", r)
		}
		seenRes[r] = true
	}
	if len(w.Tasks) == 0 {
		return fmt.Errorf("workload must contain at least one task")
	}
	seenTask := map[string]bool{}
	for _, t := range w.Tasks {
		if t.ID == "" {
			return fmt.Errorf("task ids must not be empty")
		}
		if seenTask[t.ID] {
			return fmt.Errorf("duplicate task id %q", t.ID)
		}
		seenTask[t.ID] = true
		if t.Release < 0 {
			return fmt.Errorf("task %q: release must be >= 0", t.ID)
		}
		if len(t.Ops) == 0 {
			return fmt.Errorf("task %q: ops must not be empty", t.ID)
		}
		held := map[string]bool{}
		computeSeen := 0
		for i, op := range t.Ops {
			switch op.Kind {
			case OpCompute:
				if op.Duration < 1 {
					return fmt.Errorf("task %q op %d: compute duration must be >= 1", t.ID, i)
				}
				if op.Resource != "" {
					return fmt.Errorf("task %q op %d: compute must not name a resource", t.ID, i)
				}
				computeSeen++
			case OpLock:
				if op.Resource == "" {
					return fmt.Errorf("task %q op %d: lock requires a resource name", t.ID, i)
				}
				if !seenRes[op.Resource] {
					return fmt.Errorf("task %q op %d: unknown resource %q", t.ID, i, op.Resource)
				}
				if held[op.Resource] {
					return fmt.Errorf("task %q op %d: resource %q already locked by this task (recursive locking is not supported)", t.ID, i, op.Resource)
				}
				held[op.Resource] = true
			case OpUnlock:
				if op.Resource == "" {
					return fmt.Errorf("task %q op %d: unlock requires a resource name", t.ID, i)
				}
				if !seenRes[op.Resource] {
					return fmt.Errorf("task %q op %d: unknown resource %q", t.ID, i, op.Resource)
				}
				if !held[op.Resource] {
					return fmt.Errorf("task %q op %d: cannot unlock %q: not currently held (locks must be released in stack-safe order)", t.ID, i, op.Resource)
				}
				delete(held, op.Resource)
			default:
				return fmt.Errorf("task %q op %d: unknown op kind %q", t.ID, i, op.Kind)
			}
		}
		if computeSeen == 0 {
			return fmt.Errorf("task %q: every task must contain at least one compute segment", t.ID)
		}
		if len(held) != 0 {
			names := make([]string, 0, len(held))
			for r := range held {
				names = append(names, r)
			}
			return fmt.Errorf("task %q: finishes while still holding locks %v", t.ID, names)
		}
	}
	return nil
}
