package assembler

import (
	"testing"
	"time"

	"tracestitch/internal/clock"
	"tracestitch/internal/model"
)

func fakeAssembler(cfg Config, t0 time.Time) (*Assembler, *clock.Fake) {
	fc := clock.NewFake(t0)
	return New(cfg, fc, nil, nil), fc
}

func sp(trace, id, parent, svc, name string, start, end int64) model.Span {
	return model.Span{
		TraceID: trace, SpanID: id, ParentSpanID: parent,
		ServiceName: svc, Name: name,
		StartUnixNano: start, EndUnixNano: end,
	}
}

//  1. 乱序到达：子先父后。每生成一个修订都核对包含关系，且树结构必须按
//     引用（parentSpanId）拼出，与到达顺序和时间戳无关。
func TestOutOfOrderChildBeforeParent(t *testing.T) {
	const trace = "T1"
	base := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	asm, _ := fakeAssembler(DefaultConfig(), base)

	// 故意打乱：c（孙）-> b（子）-> a（根）。
	// 时间戳也故意和父子顺序相反：c 最早，a 最晚——因果仍只看引用。
	ingest := func(s model.Span, wantReason string, wantComplete bool) model.Revision {
		t.Helper()
		res := asm.Ingest(s)
		if res.Status != "accepted" {
			t.Fatalf("span %s status=%s: %s", s.SpanID, res.Status, res.Error)
		}
		if res.Reason != wantReason {
			t.Fatalf("span %s reason=%s want %s", s.SpanID, res.Reason, wantReason)
		}
		rev, ok := asm.GetRevision(trace, res.Revision)
		if !ok {
			t.Fatalf("revision %d missing", res.Revision)
		}
		if rev.Complete != wantComplete {
			t.Fatalf("rev %d complete=%v want %v", rev.Version, rev.Complete, wantComplete)
		}
		if v := asm.CheckRevisionContainment(trace); v != nil {
			t.Fatalf("containment broken at rev %d: %v", rev.Version, v)
		}
		return rev
	}

	r1 := ingest(sp(trace, "c", "b", "svc-c", "call-b", 1_000, 2_000), ReasonInitial, false)
	if !r1.HasOrphans || !r1.MissingRoot {
		t.Fatalf("rev1: orphan=%v missingRoot=%v, want both true", r1.HasOrphans, r1.MissingRoot)
	}
	if len(r1.SpanIDs) != 1 || r1.SpanIDs[0] != "c" {
		t.Fatalf("rev1 spanIds=%v", r1.SpanIDs)
	}

	// b 到达：c 不再是孤儿，b 成为孤儿（它的父 a 还缺）。
	r2 := ingest(sp(trace, "b", "a", "svc-b", "call-a", 500, 6_000), ReasonExtended, false)
	if r2.HasOrphans != true {
		t.Fatalf("rev2 should still have orphan b, got orphans=%v", r2.HasOrphans)
	}
	if set(r2.SpanIDs)["c"] == false || set(r2.SpanIDs)["b"] == false {
		t.Fatalf("rev2 spanIds=%v must contain {b,c}", r2.SpanIDs)
	}

	// 根 a 最后到达：完整。
	r3 := ingest(sp(trace, "a", "", "svc-a", "root", 10_000, 20_000), ReasonCompleted, true)
	if r3.MissingRoot || r3.HasOrphans || r3.HasCycles {
		t.Fatalf("rev3 anomalies: missingRoot=%v orphans=%v cycles=%v",
			r3.MissingRoot, r3.HasOrphans, r3.HasCycles)
	}

	// 树必须按引用拼成 a->b->c；尽管 c 的开始时间(1000)远早于 a(10000)，
	// 深度/path 也不能受时间戳影响。
	view, _ := asm.GetTrace(trace)
	if len(view.Forest) != 1 {
		t.Fatalf("forest roots=%d want 1", len(view.Forest))
	}
	root := view.Forest[0]
	if root.Span.SpanID != "a" || root.Depth != 0 || len(root.Children) != 1 {
		t.Fatalf("root node malformed: %+v", root)
	}
	mid := root.Children[0]
	if mid.Span.SpanID != "b" || mid.Depth != 1 || len(mid.Children) != 1 {
		t.Fatalf("middle node malformed: %+v", mid)
	}
	leaf := mid.Children[0]
	if leaf.Span.SpanID != "c" || leaf.Depth != 2 {
		t.Fatalf("leaf node malformed: %+v", leaf)
	}
	if got := pathOf(leaf); got != "a/b/c" {
		t.Fatalf("path=%s want a/b/c (reference-based, not timestamp-based)", got)
	}
}

// 2. 缺根超时：缺根的 trace 超时后输出不完整标志；根迟到后产生新修订并补全。
func TestMissingRootTimeoutThenLateCompletion(t *testing.T) {
	const trace = "T2"
	base := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	cfg := Config{Timeout: 10 * time.Second, ClockSkewTolerance: time.Millisecond}
	asm, fc := fakeAssembler(cfg, base)

	// 只有子，没有根。
	res := asm.Ingest(sp(trace, "child", "root", "svc", "op", 100, 200))
	if res.Status != "accepted" {
		t.Fatalf("ingest: %s", res.Error)
	}

	if n := asm.Sweep(); n != 0 {
		t.Fatalf("sweep before deadline sealed %d traces", n)
	}

	fc.Advance(10*time.Second + time.Nanosecond)
	if n := asm.Sweep(); n != 1 {
		t.Fatalf("sweep after deadline sealed %d, want 1", n)
	}

	view, _ := asm.GetTrace(trace)
	if !view.Sealed {
		t.Fatal("trace should be sealed")
	}
	last := view.Revisions[len(view.Revisions)-1]
	if last.Reason != ReasonTimeout {
		t.Fatalf("reason=%s want timeout", last.Reason)
	}
	if last.Complete {
		t.Fatal("timeout revision must be incomplete")
	}
	if !last.MissingRoot {
		t.Fatal("timeout revision must flag missingRoot")
	}

	// 根迟到：补全生成新修订（late），包含旧集合。
	late := asm.Ingest(sp(trace, "root", "", "svc", "root", 0, 300))
	if late.Reason != ReasonLate {
		t.Fatalf("late reason=%s want late", late.Reason)
	}
	rev, _ := asm.GetRevision(trace, late.Revision)
	if !rev.Complete {
		t.Fatal("late revision should complete the trace")
	}
	if v := asm.CheckRevisionContainment(trace); v != nil {
		t.Fatalf("containment broken after late completion: %v", v)
	}
	// 补全后再次 Sweep 不能再密封。
	fc.Advance(time.Hour)
	if n := asm.Sweep(); n != 0 {
		t.Fatalf("completed trace must never be re-sealed, got %d", n)
	}
}

// 3. 重复 span：完全相同的重发幂等；载荷冲突必须被识别，首条为准。
func TestDuplicateAndConflict(t *testing.T) {
	const trace = "T3"
	base := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	asm, _ := fakeAssembler(DefaultConfig(), base)

	first := sp(trace, "a", "", "svc-a", "op-v1", 1, 2)
	if r := asm.Ingest(first); r.Status != "accepted" {
		t.Fatalf("first ingest status=%s", r.Status)
	}
	view, _ := asm.GetTrace(trace)
	revsAfterFirst := len(view.Revisions)

	// 完全相同：幂等，不产生修订。
	dup := asm.Ingest(sp(trace, "a", "", "svc-a", "op-v1", 1, 2))
	if dup.Status != "duplicate" {
		t.Fatalf("identical resend status=%s want duplicate", dup.Status)
	}
	view, _ = asm.GetTrace(trace)
	if len(view.Revisions) != revsAfterFirst {
		t.Fatal("identical duplicate must not append a revision")
	}

	// 冲突：同名 id，name 与时间戳不同。
	conflicting := sp(trace, "a", "", "svc-a", "op-v2-CONFLICT", 999, 1000)
	res := asm.Ingest(conflicting)
	if res.Status != "conflict" {
		t.Fatalf("conflict status=%s want conflict", res.Status)
	}
	if !contains(res.DifferingFields, "name") || !contains(res.DifferingFields, "startUnixNano") {
		t.Fatalf("differing fields=%v want name+startUnixNano", res.DifferingFields)
	}
	if res.Reason != ReasonConflict || res.Revision == 0 {
		t.Fatalf("conflict must append revision, got reason=%s rev=%d", res.Reason, res.Revision)
	}

	view, _ = asm.GetTrace(trace)
	// 首条为准：span 内容保持 v1。
	if view.Spans[0].Name != "op-v1" || view.Spans[0].StartUnixNano != 1 {
		t.Fatalf("first-write-wins violated: %+v", view.Spans[0])
	}
	if len(view.Conflicts) != 1 || view.Conflicts[0].SpanID != "a" {
		t.Fatalf("conflicts=%v", view.Conflicts)
	}
	if v := asm.CheckRevisionContainment(trace); v != nil {
		t.Fatalf("conflict revision must keep span set: %v", v)
	}
}

// 4. 跨服务时钟偏差：子的时钟早于父，仍按引用挂在父下；偏差只作为告警。
func TestCrossServiceClockSkewDoesNotChangeCausality(t *testing.T) {
	const trace = "T4"
	base := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	// 容差 1ms；下面偏差达 9ms。
	cfg := Config{Timeout: time.Minute, ClockSkewTolerance: time.Millisecond}
	asm, _ := fakeAssembler(cfg, base)

	// gateway 父 span：10ms..50ms（纳秒）
	parent := sp(trace, "gateway", "", "gateway", "GET /x", 10_000_000, 50_000_000)
	// auth 子 span：时钟慢了 9ms，1ms..8ms——开始早于父开始。
	childEarly := sp(trace, "auth-early", "gateway", "auth", "verify", 1_000_000, 8_000_000)
	// billing 子 span：时钟快了，结束晚于父结束。
	childLate := sp(trace, "billing-late", "gateway", "billing", "charge", 12_000_000, 80_000_000)

	for _, s := range []model.Span{parent, childEarly, childLate} {
		if r := asm.Ingest(s); r.Status != "accepted" {
			t.Fatalf("ingest %s: %s", s.SpanID, r.Error)
		}
	}

	view, _ := asm.GetTrace(trace)
	// 因果断言只看引用：两个子都挂在 gateway 下，与时钟读数无关。
	root := view.Forest[0]
	kids := map[string]*model.SpanNode{}
	for _, c := range root.Children {
		kids[c.Span.SpanID] = c
	}
	if _, ok := kids["auth-early"]; !ok {
		t.Fatal("auth-early must be a child of gateway by reference despite earlier clock")
	}
	if _, ok := kids["billing-late"]; !ok {
		t.Fatal("billing-late must be a child of gateway by reference")
	}
	if c := kids["auth-early"]; c.Depth != 1 || pathOf(c) != "gateway/auth-early" {
		t.Fatalf("auth-early position wrong: depth=%d path=%v", c.Depth, c.Path)
	}

	latest := view.Latest
	kinds := map[string]model.ClockSkewWarning{}
	for _, w := range latest.ClockSkew {
		kinds[w.Kind] = w
	}
	w1, ok := kinds["child-starts-before-parent"]
	if !ok || w1.ChildService != "auth" {
		t.Fatalf("missing child-starts-before-parent warning: %+v", latest.ClockSkew)
	}
	if w1.StartDeltaNanos != -9_000_000 {
		t.Fatalf("start delta=%d want -9000000", w1.StartDeltaNanos)
	}
	if _, ok := kinds["child-ends-after-parent"]; !ok {
		t.Fatalf("missing child-ends-after-parent warning: %+v", latest.ClockSkew)
	}
	if latest.Complete != true {
		t.Fatal("clock skew is a warning, not a structural incompleteness")
	}
}

// 5. 父链循环：自环 + 多节点环 + 汇入环的 feeder 都要被标出。
func TestParentChainCycles(t *testing.T) {
	const trace = "T5"
	base := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	asm, _ := fakeAssembler(DefaultConfig(), base)

	// a->b->c->a（ParentSpanID 表示“我的父”）
	cyc := []model.Span{
		sp(trace, "a", "b", "svc", "a", 1, 2),
		sp(trace, "b", "c", "svc", "b", 1, 2),
		sp(trace, "c", "a", "svc", "c", 1, 2),
		// feeder：f 的父是 a，整棵树汇入环，也必须标记。
		sp(trace, "f", "a", "svc", "f", 1, 2),
		// 自环。
		sp(trace, "d", "d", "svc", "d", 1, 2),
		// 正常独立树根，确保环检测不波及其它结构。
		sp(trace, "r", "", "svc", "r", 1, 2),
		sp(trace, "x", "r", "svc", "x", 1, 2),
	}
	for _, s := range cyc {
		if r := asm.Ingest(s); r.Status != "accepted" {
			t.Fatalf("ingest %s: %s", s.SpanID, r.Error)
		}
	}

	view, _ := asm.GetTrace(trace)
	if view.Complete {
		t.Fatal("trace with cycles must be incomplete")
	}
	latest := view.Latest
	if !latest.HasCycles || len(latest.CyclePath) < 3 {
		t.Fatalf("cycle not detected: %+v", latest)
	}
	// 规范环：起点为环上最小 id，且首尾闭合。
	if latest.CyclePath[0] != "a" ||
		latest.CyclePath[len(latest.CyclePath)-1] != "a" {
		t.Fatalf("canonical cycle path=%v must start/end at smallest id a", latest.CyclePath)
	}
	// 自环 d 也要在结构上标 InCycle。
	flagged := map[string]bool{}
	var walk func(n *model.SpanNode)
	walk = func(n *model.SpanNode) {
		if n.InCycle {
			flagged[n.Span.SpanID] = true
		}
		for _, c := range n.Children {
			walk(c)
		}
	}
	for _, root := range view.Forest {
		walk(root)
	}
	for _, id := range []string{"a", "b", "c", "d", "f"} {
		if !flagged[id] {
			t.Fatalf("node %s must be flagged inCycle (cycle member or feeder)", id)
		}
	}
	if flagged["r"] || flagged["x"] {
		t.Fatal("healthy subtree r/x must not be marked in cycle")
	}
	if v := asm.CheckRevisionContainment(trace); v != nil {
		t.Fatalf("containment broken: %v", v)
	}
}

// 6. 无效 span：缺 id 被拒绝，且不落库/不产生修订。
func TestInvalidSpanRejected(t *testing.T) {
	base := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	asm, _ := fakeAssembler(DefaultConfig(), base)

	r := asm.Ingest(model.Span{TraceID: "T6", Name: "no-id"})
	if r.Status != "invalid" || r.Error == "" {
		t.Fatalf("expected invalid with error, got %+v", r)
	}
	if ids := asm.ListTraces(); len(ids) != 0 {
		t.Fatalf("invalid span must not create a trace, got %v", ids)
	}
}

// ---- helpers -----------------------------------------------------------------

func set(xs []string) map[string]bool {
	m := make(map[string]bool, len(xs))
	for _, x := range xs {
		m[x] = true
	}
	return m
}

func contains(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}

func pathOf(n *model.SpanNode) string {
	out := ""
	for i, p := range n.Path {
		if i > 0 {
			out += "/"
		}
		out += p
	}
	return out
}
