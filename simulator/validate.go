package simulator

import "fmt"

// Validate checks a configuration statically. The simulator is deterministic
// and the task programs are fixed sequences, so malformed programs are
// rejected up front rather than discovered mid-run.
func Validate(cfg Config) error {
	if len(cfg.Tasks) == 0 {
		return fmt.Errorf("tasks: at least one task is required")
	}
	names := map[string]bool{}
	for i, t := range cfg.Tasks {
		if t.Name == "" {
			return fmt.Errorf("tasks[%d]: name is required", i)
		}
		if names[t.Name] {
			return fmt.Errorf("tasks[%d]: duplicate task name %q", i, t.Name)
		}
		names[t.Name] = true
		if t.ReleaseTime < 0 {
			return fmt.Errorf("task %q: releaseTime must be >= 0", t.Name)
		}
		if len(t.Steps) == 0 {
			return fmt.Errorf("task %q: at least one step is required", t.Name)
		}
		var held []string
		computeTicks := 0
		for j, s := range t.Steps {
			switch s.Op {
			case OpLock, OpUnlock:
				if s.Resource == "" {
					return fmt.Errorf("task %q step %d: resource is required", t.Name, j)
				}
			case OpCompute:
				if s.Duration <= 0 {
					return fmt.Errorf("task %q step %d: compute duration must be > 0", t.Name, j)
				}
				computeTicks += s.Duration
			default:
				return fmt.Errorf("task %q step %d: unknown op %q", t.Name, j, s.Op)
			}
			if s.Op == OpLock {
				if !contains(held, s.Resource) {
					held = append(held, s.Resource)
				}
			}
			if s.Op == OpUnlock {
				if !contains(held, s.Resource) {
					return fmt.Errorf("task %q step %d: unlock of %q that is not currently held",
						t.Name, j, s.Resource)
				}
				held = removeFirst(held, s.Resource)
			}
		}
		if len(held) != 0 {
			return fmt.Errorf("task %q: ends while still holding %v", t.Name, held)
		}
		if computeTicks == 0 {
			return fmt.Errorf("task %q: program must contain at least one compute step", t.Name)
		}
	}
	if cfg.MaxTime < 0 {
		return fmt.Errorf("maxTime must be >= 0")
	}
	return nil
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

func removeFirst(xs []string, x string) []string {
	for i, v := range xs {
		if v == x {
			return append(xs[:i], xs[i+1:]...)
		}
	}
	return xs
}
