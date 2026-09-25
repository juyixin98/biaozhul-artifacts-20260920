package analyzer

import (
	"testing"

	"cpathtrace/internal/model"
	"cpathtrace/internal/synthetic"
)

func mustTrace(t *testing.T, name string) model.Trace {
	t.Helper()
	tr, ok := synthetic.Build(name)
	if !ok {
		t.Fatalf("missing synthetic scenario %q", name)
	}
	for i := range tr.Spans {
		if err := tr.Spans[i].Normalize(); err != nil {
			t.Fatalf("normalize: %v", err)
		}
	}
	return tr
}

func row(r *Result, id string) SpanTime {
	for _, row := range r.SpanTimes {
		if row.SpanID == id {
			return row
		}
	}
	return SpanTime{}
}

func hasDiag(r *Result, code string) bool {
	for _, d := range r.Diagnostics {
		if d.Code == code {
			return true
		}
	}
	return false
}

func diagSpan(r *Result, code string) string {
	for _, d := range r.Diagnostics {
		if d.Code == code {
			return d.SpanID
		}
	}
	return ""
}

func TestSerialParallelHandCalculation(t *testing.T) {
	// Hand calculation (see README "手算样例"):
	// self: root=0 s1=20 a1=0 x=50 y=50 a2=30 s2=20
	// C:    x=50 y=50 a1=100 a2=30 s1=20 s2=20
	// C(root) = 0 + 20 + 20 + max(100,30) = 140
	r := Analyze(mustTrace(t, synthetic.SerialParallel))

	if r.RootSpanID != "root" {
		t.Fatalf("root = %q, want root", r.RootSpanID)
	}
	if r.CriticalPathDuration == nil || *r.CriticalPathDuration != 140 {
		t.Fatalf("critical path = %v, want 140", r.CriticalPathDuration)
	}
	if r.TraceDuration == nil || *r.TraceDuration != 120 {
		t.Fatalf("trace duration = %v, want 120", r.TraceDuration)
	}
	if r.Ratio != 1.1667 {
		t.Fatalf("ratio = %v, want 1.1667", r.Ratio)
	}

	wantSelf := map[string]int64{
		"root": 0, "s1": 20, "a1": 0, "x": 50, "y": 50, "a2": 30, "s2": 20,
	}
	for id, want := range wantSelf {
		if got := row(r, id).SelfTime; got != want {
			t.Errorf("self(%s) = %d, want %d", id, got, want)
		}
	}
	wantCritical := map[string]int64{
		"root": 140, "s1": 20, "a1": 100, "x": 50, "y": 50, "a2": 30, "s2": 20,
	}
	for id, want := range wantCritical {
		got := row(r, id).CriticalTime
		if got == nil || *got != want {
			t.Errorf("critical(%s) = %v, want %d", id, got, want)
		}
	}

	// Path is a DFS over the critical-path tree: root, sync s1, then
	// the winning async subtree a1 with its own sync chain x->y, then
	// the remaining sync s2. Losing async a2 must NOT appear.
	var pathIDs []string
	for _, p := range r.CriticalPath {
		pathIDs = append(pathIDs, p.SpanID)
	}
	wantPath := []string{"root", "s1", "a1", "x", "y", "s2"}
	if len(pathIDs) != len(wantPath) {
		t.Fatalf("path = %v, want %v", pathIDs, wantPath)
	}
	for i := range wantPath {
		if pathIDs[i] != wantPath[i] {
			t.Errorf("path = %v, want %v", pathIDs, wantPath)
			break
		}
	}
	if pathIDs[0] != "root" {
		t.Errorf("path must start at root: %v", pathIDs)
	}
	// Crit exceeds duration fires only for root (140 > 120): a1's C is
	// 100 == duration 100, so no warning there.
	if !hasDiag(r, CodeCriticalExceedsDuration) {
		t.Errorf("expected CRITICAL_EXCEEDS_DURATION on root")
	}
	if diagSpan(r, CodeCriticalExceedsDuration) != "root" {
		t.Errorf("CRITICAL_EXCEEDS_DURATION span = %q, want root",
			diagSpan(r, CodeCriticalExceedsDuration))
	}
}

func TestOverlappingIntervalsUnionSelfTime(t *testing.T) {
	// sync_overlap: child intervals cover [0,60) U [10,90) U [40,100)
	// = [0,100): root self = 0 despite three overlapping spans.
	r := Analyze(mustTrace(t, synthetic.SyncOverlap))
	if got := row(r, "root").SelfTime; got != 0 {
		t.Fatalf("self(root) = %d, want 0 (union, not sum)", got)
	}
	// Sync overlap is detected once, anchored on the later child.
	if diagSpan(r, CodeSyncOverlap) != "s2" {
		t.Fatalf("SYNC_CHILD_OVERLAP span = %q, want s2", diagSpan(r, CodeSyncOverlap))
	}
	// C(root) = 0 + 60 + 60 + 80 = 200 > duration 100.
	if r.CriticalPathDuration == nil || *r.CriticalPathDuration != 200 {
		t.Fatalf("critical = %v, want 200", r.CriticalPathDuration)
	}
	if !hasDiag(r, CodeCriticalExceedsDuration) {
		t.Errorf("expected CRITICAL_EXCEEDS_DURATION")
	}
}

func TestSelfTimeNeverSumsChildren(t *testing.T) {
	// Two parallel async children fully cover the parent but overlap
	// each other: naive sum would yield self=-40; union yields 0.
	tr := model.Trace{TraceID: "t", Spans: []model.Span{
		{SpanID: "p", Name: "p", StartTime: 0, EndTime: 100, Relation: "sync"},
		{SpanID: "a", ParentSpanID: "p", Name: "a", StartTime: 0, EndTime: 80, Relation: "async"},
		{SpanID: "b", ParentSpanID: "p", Name: "b", StartTime: 20, EndTime: 100, Relation: "async"},
	}}
	for i := range tr.Spans {
		_ = tr.Spans[i].Normalize()
	}
	r := Analyze(tr)
	if got := row(r, "p").SelfTime; got != 0 {
		t.Fatalf("self(p) = %d, want 0 (overlapping async children merged once)", got)
	}
	// C(p) = 0 + max(C(a)=80, C(b)=80) = 80; tie picks the first id.
	if *r.CriticalPathDuration != 80 {
		t.Fatalf("critical = %v, want 80", r.CriticalPathDuration)
	}
	// Self times of a leaf and a zero-duration span.
	if got := row(r, "a").SelfTime; got != 80 {
		t.Fatalf("self(a) = %d, want 80", got)
	}
}

func TestSelfTimeClipping(t *testing.T) {
	// clock_skew: c1 [-20,30) clips to [0,30)=30; c2 [60,140) clips
	// to [60,100)=40; self(root) = 100-70 = 30.
	r := Analyze(mustTrace(t, synthetic.ClockSkew))
	if got := row(r, "root").SelfTime; got != 30 {
		t.Fatalf("self(root) = %d, want 30", got)
	}
	if *r.CriticalPathDuration != 160 {
		t.Fatalf("critical = %v, want 160 (30 + 50 sync + 80 async max)",
			r.CriticalPathDuration)
	}
	// Both children are outside the envelope.
	outCount := 0
	for _, d := range r.Diagnostics {
		if d.Code == CodeChildOutsideParent {
			outCount++
		}
	}
	if outCount != 2 {
		t.Fatalf("CHILD_OUTSIDE_PARENT count = %d, want 2", outCount)
	}
}

func TestMissingSpan(t *testing.T) {
	r := Analyze(mustTrace(t, synthetic.MissingSpan))
	if diagSpan(r, CodeMissingParent) != "o" {
		t.Fatalf("MISSING_PARENT span = %q, want o", diagSpan(r, CodeMissingParent))
	}
	// Genuine root "root" is selected, not the orphan "o".
	if r.RootSpanID != "root" {
		t.Fatalf("root = %q, want root", r.RootSpanID)
	}
	// Orphan subtree must not feed the selected root path.
	for _, p := range r.CriticalPath {
		if p.SpanID == "o" {
			t.Fatalf("orphan span o must not appear in critical path")
		}
	}
	// C(root) = self70 + C(c)=30 = 100.
	if *r.CriticalPathDuration != 100 {
		t.Fatalf("critical = %v, want 100", r.CriticalPathDuration)
	}
	if got := row(r, "root").SelfTime; got != 70 {
		t.Fatalf("self(root) = %d, want 70 (c covers 10..40 only)", got)
	}
	if r.HasError() {
		t.Fatalf("missing parent is a warning, not error")
	}
}

func TestCycle(t *testing.T) {
	r := Analyze(mustTrace(t, synthetic.Cycle))
	if !hasDiag(r, CodeCycle) {
		t.Fatalf("expected CYCLE diagnostic")
	}
	if !r.HasError() {
		t.Fatalf("cycle must be severity=error")
	}
	if r.CriticalPathDuration != nil {
		t.Fatalf("critical path must be null on cycle, got %d", *r.CriticalPathDuration)
	}
	if len(r.CriticalPath) != 0 {
		t.Fatalf("critical path must be empty on cycle")
	}
	if r.RootSpanID != "r" {
		t.Fatalf("root = %q, want r even when a separate cycle exists", r.RootSpanID)
	}
}

func TestPureCycleNoRoot(t *testing.T) {
	tr := model.Trace{TraceID: "t", Spans: []model.Span{
		{SpanID: "a", ParentSpanID: "c", Name: "a", StartTime: 0, EndTime: 1},
		{SpanID: "b", ParentSpanID: "a", Name: "b", StartTime: 0, EndTime: 1},
		{SpanID: "c", ParentSpanID: "b", Name: "c", StartTime: 0, EndTime: 1},
	}}
	r := Analyze(tr)
	if !hasDiag(r, CodeCycle) || !hasDiag(r, CodeNoRoot) {
		t.Fatalf("want CYCLE + NO_ROOT, got %+v", r.Diagnostics)
	}
}

func TestNegativeDurationAndClockContradictions(t *testing.T) {
	tr := model.Trace{TraceID: "t", Spans: []model.Span{
		{SpanID: "p", Name: "p", StartTime: 100, EndTime: 0},
	}}
	r := Analyze(tr)
	if !hasDiag(r, CodeNegativeDuration) {
		t.Fatalf("want NEGATIVE_DURATION")
	}
	zero := model.Trace{TraceID: "z", Spans: []model.Span{
		{SpanID: "p", Name: "p", StartTime: 5, EndTime: 5},
	}}
	rz := Analyze(zero)
	if !hasDiag(rz, CodeZeroDuration) {
		t.Fatalf("want ZERO_DURATION")
	}
	if rz.Ratio != 0 || rz.TraceDuration == nil {
		t.Fatalf("zero-duration root ratio must be 0 with duration present")
	}
}

func TestMultipleRootsPicksEarliest(t *testing.T) {
	tr := model.Trace{TraceID: "t", Spans: []model.Span{
		{SpanID: "late", Name: "late", StartTime: 50, EndTime: 60},
		{SpanID: "early", Name: "early", StartTime: 10, EndTime: 20},
	}}
	r := Analyze(tr)
	if r.RootSpanID != "early" {
		t.Fatalf("root = %q, want early", r.RootSpanID)
	}
	if !hasDiag(r, CodeMultipleRoots) {
		t.Fatalf("want MULTIPLE_ROOTS warning")
	}
}

func TestAsyncMaxNotSum(t *testing.T) {
	// Parent self works 0..10; two overlapping async subtrees cover
	// [10,40)=30 and [10,100)=90. Critical = 10 + max(30,90) = 100,
	// never the summed 130.
	tr := model.Trace{TraceID: "t", Spans: []model.Span{
		{SpanID: "p", Name: "p", StartTime: 0, EndTime: 100},
		{SpanID: "a", ParentSpanID: "p", Name: "a", StartTime: 10, EndTime: 40, Relation: "async"},
		{SpanID: "b", ParentSpanID: "p", Name: "b", StartTime: 10, EndTime: 100, Relation: "async"},
	}}
	r := Analyze(tr)
	if got := row(r, "p").SelfTime; got != 10 {
		t.Fatalf("self(p) = %d, want 10", got)
	}
	if *r.CriticalPathDuration != 100 {
		t.Fatalf("critical = %v, want 100", r.CriticalPathDuration)
	}
	foundB := false
	for _, p := range r.CriticalPath {
		if p.SpanID == "a" {
			t.Fatalf("losing async branch a must not be on the path")
		}
		if p.SpanID == "b" {
			foundB = true
		}
	}
	if !foundB {
		t.Fatalf("winning async branch b must be on the path")
	}
}

func TestSyncSerialSum(t *testing.T) {
	// Two non-overlapping sync children: 20 + 30 serialize with self 50.
	tr := model.Trace{TraceID: "t", Spans: []model.Span{
		{SpanID: "p", Name: "p", StartTime: 0, EndTime: 100},
		{SpanID: "a", ParentSpanID: "p", Name: "a", StartTime: 0, EndTime: 20},
		{SpanID: "b", ParentSpanID: "p", Name: "b", StartTime: 20, EndTime: 50},
	}}
	r := Analyze(tr)
	if got := row(r, "p").SelfTime; got != 50 {
		t.Fatalf("self(p) = %d, want 50", got)
	}
	if *r.CriticalPathDuration != 100 {
		t.Fatalf("critical = %v, want 100", r.CriticalPathDuration)
	}
	if hasDiag(r, CodeSyncOverlap) {
		t.Fatalf("touching boundaries (end==start) must not overlap")
	}
}
