package check

import "raftlab/internal/sim"

// Shrink reduces a violating trace greedily: it repeatedly tries removing
// individual actions and shrinking run-lengths, keeping the change whenever
// the same kind of violation is still produced. The result is a local minimum
// that is much easier to read when replaying a counterexample.
func Shrink(trace []Action, kind string, cc sim.ClusterConfig) []Action {
	r := NewRunner(cc)
	fails := func(t []Action) bool {
		_, res := r.Run(t)
		return !res.OK() && res.Violation.Kind == kind
	}
	if !fails(trace) {
		return nil
	}

	current := append([]Action(nil), trace...)
	for changed := true; changed; {
		changed = false

		// 1. Remove individual actions.
		for i := 0; i < len(current); i++ {
			cand := append(append([]Action(nil), current[:i]...), current[i+1:]...)
			if fails(cand) {
				current = cand
				changed = true
				i--
			}
		}

		// 2. Halve run-lengths ("run": N ticks -> N/2 ... 1).
		for i := 0; i < len(current); i++ {
			if current[i].Op != ActRun || current[i].Node <= 1 {
				continue
			}
			half := current[i].Node / 2
			if half == 0 {
				half = 1
			}
			cand := append([]Action(nil), current...)
			cand[i] = Action{Op: ActRun, Node: half}
			if fails(cand) {
				current = cand
				changed = true
			}
		}

		// 3. Merge adjacent run actions, then try removing the merged block.
		for i := 0; i+1 < len(current); i++ {
			if current[i].Op == ActRun && current[i+1].Op == ActRun {
				merged := Action{Op: ActRun, Node: current[i].Node + current[i+1].Node}
				cand := append(append([]Action(nil), current[:i]...), merged)
				cand = append(cand, current[i+2:]...)
				if fails(cand) {
					current = cand
					changed = true
					i--
				}
			}
		}
	}
	return current
}
