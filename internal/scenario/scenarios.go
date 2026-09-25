// Package scenario contains the canonical workloads used for demonstration
// and acceptance tests.
package scenario

import "pim/internal/program"

import "pim/internal/scheduler"

// cpu, lock, unlock are shorthand constructors for readability.
func cpu(n int64) program.Action      { return program.Action{CPU: n} }
func lock(id string) program.Action   { return program.Action{Acquire: id} }
func unlock(id string) program.Action { return program.Action{Release: id} }
func task(id string, arrival int64, pri int, a ...program.Action) scheduler.TaskSpec {
	return scheduler.TaskSpec{ID: id, Arrival: arrival, Priority: pri, Program: program.NewScript(a...)}
}

// ClassicInversion builds the textbook unbounded priority-inversion example
// (Lampson & Redell; Sha et al. §1).
//
//   - Low (pri 1) arrives at t=0, takes R at tick 1 and needs 6 ticks inside.
//   - High (pri 3) arrives at t=2 and blocks on R.
//   - Medium (pri 2) arrives at t=4 and — WITHOUT inheritance — preempts Low
//     while High is blocked, so High's blocking time is extended by Medium.
//
// With inheritance disabled the returned spec reproduces the inversion;
// enable it to see the fix (Low inherits priority 3 and Medium cannot run).
func ClassicInversion(inherit bool) scheduler.Spec {
	return scheduler.Spec{
		Name:      "classic-priority-inversion",
		Resources: []string{"R"},
		Options:   scheduler.Options{PriorityInheritance: inherit},
		Tasks: []scheduler.TaskSpec{
			task("Low", 0, 1, cpu(1), lock("R"), cpu(6), unlock("R"), cpu(1)),
			task("Medium", 4, 2, cpu(4)),
			task("High", 2, 3, lock("R"), cpu(2), unlock("R")),
		},
	}
}

// ThreeLevelInheritance builds the nested-chain case demonstrating THREE
// priority levels with TWO-hop transitive inheritance. The realised
// blocking chain after arrivals settle at t=2 is:
//
//	Urgent (pri 4) -- waits on R2 --> High (pri 3) -- waits on R1 --> Low (pri 1)
//
// Effective-priority transitions observed in the run:
//
//	Low : 1 -> 3 (High blocks on R1) -> 4 (Urgent blocks on R2) -> 1 (release)
//	High: 3 -> 4 (Urgent blocks on R2)                    -> 3 (release)
func ThreeLevelInheritance() scheduler.Spec {
	return scheduler.Spec{
		Name:      "three-level-transitive-inheritance",
		Resources: []string{"R1", "R2"},
		Options:   scheduler.Options{PriorityInheritance: true},
		Tasks: []scheduler.TaskSpec{
			// Low is alone at t=0 and takes R1 immediately.
			task("Low", 0, 1, lock("R1"), cpu(8), unlock("R1")),
			// High arrives at t=1: takes R2, then blocks on R1 held by Low.
			task("High", 1, 3, lock("R2"), lock("R1"), cpu(2),
				unlock("R1"), unlock("R2")),
			// Urgent arrives at t=2: blocks on R2 held by blocked High;
			// Low must transitively receive priority 4.
			task("Urgent", 2, 4, lock("R2"), cpu(2), unlock("R2")),
		},
	}
}

// MultiLockReleaseOrder exercises a task holding TWO locks simultaneously
// with waiters on each, then releasing them in both orders across two
// otherwise identical runs.
//
//   - Low (pri 1) acquires R1 then R2 and holds both while doing work.
//   - A (pri 3) blocks on R1 at t=2; B (pri 4) blocks on R2 at t=3.
//   - When PI is enabled, Low inherits 4.
//
// The release sequence is selected by order ("R1-first" or "R2-first"):
// the grant/wakeup event ordering and Low's effective-priority restoration
// differ between the two.
func MultiLockReleaseOrder(order string) scheduler.Spec {
	var low []program.Action
	low = append(low, lock("R1"), lock("R2"), cpu(6))
	switch order {
	case "R2-first":
		low = append(low, unlock("R2"), cpu(1), unlock("R1"))
	default: // "R1-first"
		low = append(low, unlock("R1"), cpu(1), unlock("R2"))
	}
	low = append(low, cpu(1))
	return scheduler.Spec{
		Name:      "multi-lock-release-" + order,
		Resources: []string{"R1", "R2"},
		Options:   scheduler.Options{PriorityInheritance: true},
		Tasks: []scheduler.TaskSpec{
			task("Low", 0, 1, low...),
			task("A", 2, 3, lock("R1"), cpu(1), unlock("R1")),
			task("B", 3, 4, lock("R2"), cpu(1), unlock("R2")),
		},
	}
}

// DeadlockAB is the classic two-task AB/BA deadlock. Detection halts the
// simulation and emits a deadlock event carrying the cycle.
//
// Construction (priority inheritance cannot prevent this one, which is the
// teaching point):
//
//   - t0: T1 (pri 3) takes R1 and starts working.
//   - t1: T2 (pri 4) arrives, preempts T1, takes R2, then blocks on R1.
//   - T1 inherits priority 4, resumes, and requests R2 — held by blocked T2.
//     The wait-for graph now contains the cycle T1 -> T2 -> T1.
func DeadlockAB() scheduler.Spec {
	return scheduler.Spec{
		Name:      "ab-ba-deadlock",
		Resources: []string{"R1", "R2"},
		Options:   scheduler.Options{PriorityInheritance: true},
		Tasks: []scheduler.TaskSpec{
			task("T1", 0, 3, lock("R1"), cpu(1), lock("R2"),
				cpu(1), unlock("R2"), unlock("R1")),
			task("T2", 1, 4, lock("R2"), lock("R1"),
				cpu(1), unlock("R1"), unlock("R2")),
		},
	}
}

// InheritanceThenRestore is the simplest single-level inheritance case:
// Low inherits High's priority, completes the critical section, and drops
// back to its base priority (effective priority recomputed on release).
func InheritanceThenRestore() scheduler.Spec {
	return scheduler.Spec{
		Name:      "inherit-then-restore",
		Resources: []string{"R"},
		Options:   scheduler.Options{PriorityInheritance: true},
		Tasks: []scheduler.TaskSpec{
			task("Low", 0, 1, cpu(1), lock("R"), cpu(3), unlock("R"), cpu(2)),
			task("High", 2, 4, lock("R"), cpu(1), unlock("R")),
		},
	}
}

// All returns every built-in scenario under stable names; the HTTP layer
// serves these by name.
func All() map[string]func() scheduler.Spec {
	return map[string]func() scheduler.Spec{
		"classic-inversion-no-pi": func() scheduler.Spec { return ClassicInversion(false) },
		"classic-inversion-pi":    func() scheduler.Spec { return ClassicInversion(true) },
		"three-level-inheritance": ThreeLevelInheritance,
		"multi-lock-r1-first":     func() scheduler.Spec { return MultiLockReleaseOrder("R1-first") },
		"multi-lock-r2-first":     func() scheduler.Spec { return MultiLockReleaseOrder("R2-first") },
		"deadlock-ab-ba":          DeadlockAB,
		"inherit-restore":         InheritanceThenRestore,
	}
}
