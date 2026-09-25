// Package synthetic builds the hand-computable demo traces used by the
// POST /api/synthetic/seed endpoint, the CLI and the examples.
package synthetic

import "cpathtrace/internal/model"

// Names of the available scenarios.
const (
	SerialParallel = "serial_parallel" // sync sum + async max, normal
	SyncOverlap    = "sync_overlap"    // two sync children overlap -> warning
	MissingSpan    = "missing_span"    // dangling parent reference
	Cycle          = "cycle"           // parent cycle -> error, no path
	ClockSkew      = "clock_skew"      // child outside parent interval
)

// Names lists all scenarios in stable order.
func Names() []string {
	return []string{SerialParallel, SyncOverlap, MissingSpan, Cycle, ClockSkew}
}

func span(id, parent, name string, start, end int64, relation string) model.Span {
	return model.Span{
		SpanID:       id,
		ParentSpanID: parent,
		Name:         name,
		StartTime:    start,
		EndTime:      end,
		Relation:     relation,
	}
}

// Build returns one synthetic trace. ok is false for an unknown name.
func Build(name string) (model.Trace, bool) {
	switch name {
	case SerialParallel:
		// root [0,120]
		//   s1 (sync) [0,20]
		//   async a1 [20,120] -> sync x [20,70] (50), y [70,120] (50)  => C(a1)=100
		//   async a2 [20,50]  (self 30)                                 => C(a2)=30
		//   s2 (sync) [50,70] (self 20)
		// root self = 120 - union(child intervals) = 120-120 = 0
		// C(root) = 0 + C(s1)=20 + C(s2)=20 + max(C(a1)=100, C(a2)=30) = 140
		return model.Trace{TraceID: "demo-serial-parallel", Spans: []model.Span{
			span("root", "", "gateway", 0, 120, "sync"),
			span("s1", "root", "auth", 0, 20, "sync"),
			span("a1", "root", "fanout-A", 20, 120, "async"),
			span("x", "a1", "A-stage1", 20, 70, "sync"),
			span("y", "a1", "A-stage2", 70, 120, "sync"),
			span("a2", "root", "fanout-B", 20, 50, "async"),
			span("s2", "root", "render", 50, 70, "sync"),
		}}, true

	case SyncOverlap:
		// root [0,100]; sync children s1 [0,60] and s2 [40,100] overlap
		// [40,60) => SYNC_CHILD_OVERLAP. Self union = 0..100 = 100.
		// C(root)=100 too; async a3 [10,90] (self 80) is parallel:
		// C(root) = self0 + C(s1)60 + C(s2)60 + max async 80 = 200 >
		// duration 100 => CRITICAL_EXCEEDS_DURATION (relations or clock
		// are inconsistent).
		return model.Trace{TraceID: "demo-sync-overlap", Spans: []model.Span{
			span("root", "", "gateway", 0, 100, "sync"),
			span("s1", "root", "db-query-1", 0, 60, "sync"),
			span("s2", "root", "db-query-2", 40, 100, "sync"),
			span("a3", "root", "bg-flush", 10, 90, "async"),
		}}, true

	case MissingSpan:
		// root [0,100], sync c [10,40]; orphan o [40,80] names parent
		// "ghost" which is absent. o is analyzed as an extra root and
		// does NOT contribute to the selected root's path.
		return model.Trace{TraceID: "demo-missing-span", Spans: []model.Span{
			span("root", "", "gateway", 0, 100, "sync"),
			span("c", "root", "cache-get", 10, 40, "sync"),
			span("o", "ghost", "orphaned-call", 40, 80, "sync"),
		}}, true

	case Cycle:
		// a -> b -> c -> a is a cycle (b's parent is a, c's b, a's c),
		// disconnected from the reachable root r [0,30]. A cycle
		// anywhere invalidates the analysis => error, no critical path.
		return model.Trace{TraceID: "demo-cycle", Spans: []model.Span{
			span("r", "", "gateway", 0, 30, "sync"),
			span("a", "c", "svc-a", 0, 30, "sync"),
			span("b", "a", "svc-b", 0, 30, "sync"),
			span("c", "b", "svc-c", 0, 30, "sync"),
		}}, true

	case ClockSkew:
		// root [0,100]; sync child c1 starts BEFORE root (-20..30) and
		// async c2 ends AFTER root (60..140): two CHILD_OUTSIDE_PARENT
		// warnings. Clipped child intervals: c1 [0,30]=30, c2
		// [60,100]=40, self = 100-70 = 30.
		// C(root) = 30 + C(c1)=50 + max(C(c2)=80) = 160.
		return model.Trace{TraceID: "demo-clock-skew", Spans: []model.Span{
			span("root", "", "gateway", 0, 100, "sync"),
			span("c1", "root", "early-call", -20, 30, "sync"),
			span("c2", "root", "late-call", 60, 140, "async"),
		}}, true
	}
	return model.Trace{}, false
}
