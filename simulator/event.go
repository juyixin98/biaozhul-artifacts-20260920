package simulator

import (
	"fmt"
	"strings"
)

// String renders one event as a compact decision-log line.
func (ev Event) String() string {
	var b strings.Builder
	fmt.Fprintf(&b, "t=%-3d %-17s", ev.Time, ev.Kind)
	if ev.Task != "" {
		fmt.Fprintf(&b, " task=%s", ev.Task)
	}
	if ev.Resource != "" {
		fmt.Fprintf(&b, " res=%s", ev.Resource)
	}
	if ev.Holder != "" {
		fmt.Fprintf(&b, " holder=%s", ev.Holder)
	}
	if ev.Kind == evPriorityBoost || ev.Kind == evPriorityDrop {
		fmt.Fprintf(&b, " prio %d->%d", ev.OldPriority, ev.NewPriority)
		if ev.CausedBy != "" {
			fmt.Fprintf(&b, " due=%s", ev.CausedBy)
		}
	}
	if ev.Kind == evSchedule {
		fmt.Fprintf(&b, " cands=[%s]", strings.Join(ev.Candidates, ","))
		if len(ev.CandPriorities) > 0 {
			parts := make([]string, 0, len(ev.CandPriorities))
			for _, c := range ev.Candidates {
				parts = append(parts, fmt.Sprintf("%s:%d", c, ev.CandPriorities[c]))
			}
			fmt.Fprintf(&b, " eff=[%s]", strings.Join(parts, ","))
		}
		if ev.Preempted != "" {
			fmt.Fprintf(&b, " preempted=%s", ev.Preempted)
		}
		if ev.TickConsumed {
			b.WriteString(" tick=yes")
		}
	}
	if ev.Kind == evCompute {
		fmt.Fprintf(&b, " remaining=%d", ev.Remaining)
	}
	if ev.Kind == evIdle {
		fmt.Fprintf(&b, " ->t=%d", ev.JumpTo)
	}
	if len(ev.Cycle) > 0 {
		fmt.Fprintf(&b, " cycle=%v", ev.Cycle)
	}
	if ev.Message != "" {
		fmt.Fprintf(&b, " (%s)", ev.Message)
	}
	return b.String()
}

// Trace renders an entire event trace as newline-separated decision lines.
func Trace(res Result) string {
	var b strings.Builder
	for _, ev := range res.Events {
		b.WriteString(ev.String())
		b.WriteByte('\n')
	}
	return b.String()
}
