package scheduler

import (
	"fmt"
	"strings"
)

// TimelineText renders a compact, human-readable scheduling timeline from a
// run report. It is a presentation helper; the JSON report remains the
// machine-readable source of truth.
func TimelineText(r *Report) string {
	var b strings.Builder
	fmt.Fprintf(&b, "mode=%s queue=%s endTick=%d completed=%t deadlocked=%t",
		r.Inheritance, r.QueuePolicy, r.EndTick, r.Completed, r.Deadlocked)
	if r.Fatal != "" {
		fmt.Fprintf(&b, " fatal=%q", r.Fatal)
	}
	b.WriteString("\n")
	for _, e := range r.Events {
		fmt.Fprintf(&b, "t=%-3d seq=%-3d %-14s", e.Tick, e.Seq, e.Type)
		b.WriteString(" " + renderEvent(e))
		b.WriteString("\n")
	}
	b.WriteString("---- tasks ----\n")
	for _, t := range r.Tasks {
		fmt.Fprintf(&b, "  %s base=%d eff=%d state=%s cpu=%d blocked=%d ready=%d finish=%d holds=%v boosts=%d\n",
			t.ID, t.BasePriority, t.Effective, t.State, t.CPUTicks, t.BlockedTicks,
			t.ReadyTicks, t.FinishTick, t.HeldLocks, t.PriorityBoosts)
	}
	b.WriteString("---- locks ----\n")
	for _, l := range r.Locks {
		holder := l.Holder
		if holder == "" {
			holder = "-"
		}
		fmt.Fprintf(&b, "  %s holder=%s waiters=%v\n", l.ID, holder, l.Waiters)
	}
	return b.String()
}

func renderEvent(e Event) string {
	var parts []string
	add := func(k, v string) {
		if v != "" {
			parts = append(parts, k+"="+v)
		}
	}
	add("task", e.Task)
	add("lock", e.Lock)
	add("donor", e.Donor)
	add("owner", e.Owner)
	add("by", e.By)
	add("to", e.To)
	if e.Old != 0 || e.New != 0 {
		parts = append(parts, fmt.Sprintf("prio %d->%d", e.Old, e.New))
	}
	if e.Type == EvCPUTick {
		parts = append(parts, fmt.Sprintf("rem=%d eff=%d", e.Remaining, e.Effective))
	}
	if e.Delta != 0 {
		parts = append(parts, fmt.Sprintf("delta=%d", e.Delta))
	}
	if len(e.Cycle) > 0 {
		parts = append(parts, "cycle="+strings.Join(e.Cycle, "->"))
	}
	if len(e.Waiters) > 0 {
		parts = append(parts, "waiters=["+strings.Join(e.Waiters, ",")+"]")
	}
	add("reason", e.Reason)
	return strings.Join(parts, " ")
}
