package trace

import (
	"encoding/json"
	"slices"
	"testing"
	"time"
)

var testT0 = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

func tspan(traceID, id, parent, svc, op string, recvNS int64, startMS, durMS int64) Span {
	return Span{
		TraceID:       traceID,
		SpanID:        id,
		ParentSpanID:  parent,
		Service:       svc,
		Operation:     op,
		Start:         testT0.Add(time.Duration(startMS) * time.Millisecond),
		DurationNanos: (time.Duration(durMS) * time.Millisecond).Nanoseconds(),
		ReceiveNS:     recvNS,
	}
}

func mustIngest(t *testing.T, a *Assembler, spans ...Span) []IngestResult {
	t.Helper()
	res, _, err := a.IngestBatch(spans, -1)
	if err != nil {
		t.Fatalf("IngestBatch: %v", err)
	}
	return res
}

func mustAdvance(t *testing.T, a *Assembler, ns int64) []Revision {
	t.Helper()
	revs, err := a.AdvanceWatermark(ns)
	if err != nil {
		t.Fatalf("AdvanceWatermark: %v", err)
	}
	return revs
}

// 1. 乱序 + 子先父后：图结构按 parent 指针拼装，与摄入顺序、本地时间无关。
func TestOutOfOrderChildBeforeParent(t *testing.T) {
	a := New(Config{TimeoutNS: 100, SkewToleranceNS: 1000}, nil)
	const tr = "tr1"
	// 到达顺序：孙 → 子 → 根。
	mustIngest(t, a,
		tspan(tr, "g", "c", "edge", "render", 30, 30, 5),
		tspan(tr, "c", "root", "api", "call", 20, 10, 40),
		tspan(tr, "root", "", "gw", "entry", 10, 0, 100),
	)
	rev, ok, live := a.GetTrace(tr)
	if !ok || !live {
		t.Fatalf("expected live trace, ok=%v live=%v", ok, live)
	}
	if !rev.Complete {
		t.Fatalf("expected complete, reasons=%v missing=%v", rev.Reasons, rev.MissingParents)
	}
	if len(rev.RootSpanIDs) != 1 || rev.RootSpanIDs[0] != "root" {
		t.Fatalf("root wrong: %v", rev.RootSpanIDs)
	}
	// 到达顺序被保留（体现乱序拼装），但结构上根唯一。
	if got := rev.SpanIDs; len(got) != 3 || got[0] != "g" || got[2] != "root" {
		t.Fatalf("arrival order wrong: %v", got)
	}
}

// 2. 缺根 → 超时输出不完整标志；根迟到 → 重开新修订；逐版包含。
func TestMissingRootTimeoutThenLateRevision(t *testing.T) {
	a := New(Config{TimeoutNS: 100, SkewToleranceNS: 1000}, nil)
	const tr = "tr-missing"
	mustIngest(t, a,
		tspan(tr, "c", "root", "api", "call", 10, 10, 40),
		tspan(tr, "g", "c", "edge", "render", 20, 30, 5),
	)

	revs := mustAdvance(t, a, 200)
	if len(revs) != 1 {
		t.Fatalf("want 1 timeout revision, got %d", len(revs))
	}
	rev1 := revs[0]
	if rev1.Complete || !rev1.Closed {
		t.Fatalf("rev1 must be closed+incomplete: %+v", rev1)
	}
	if !slices.Contains(rev1.Reasons, ReasonTimeout) ||
		!slices.Contains(rev1.Reasons, "missing-root") ||
		!slices.Contains(rev1.Reasons, ReasonMissingParent) {
		t.Fatalf("rev1 reasons wrong: %v", rev1.Reasons)
	}
	if rev1.Revision != 1 {
		t.Fatalf("rev1 number = %d", rev1.Revision)
	}

	// 关闭后查询应返回冻结快照，不被后续修改污染（先查一次留档）。
	rev1Frozen, _, live := a.GetTrace(tr)
	if live {
		t.Fatal("trace should be closed")
	}

	// 迟到的根。
	mustIngest(t, a, tspan(tr, "root", "", "gw", "entry", 300, 0, 100))
	revs2 := mustAdvance(t, a, 500)
	if len(revs2) != 1 {
		t.Fatalf("want 1 revised revision, got %d", len(revs2))
	}
	rev2 := revs2[0]
	if !rev2.Complete {
		t.Fatalf("rev2 must be complete, reasons=%v", rev2.Reasons)
	}
	if rev2.Revision != 2 || rev2.RevisedOf != 1 {
		t.Fatalf("rev2 linkage wrong: rev=%d revisedOf=%d", rev2.Revision, rev2.RevisedOf)
	}
	if !slices.Contains(rev2.Reasons, ReasonRevised) {
		t.Fatalf("rev2 must cite late-arrival-revision: %v", rev2.Reasons)
	}
	// 逐版包含关系：rev2 必须包含 rev1 的全部 span。
	for _, id := range rev1Frozen.SpanIDs {
		if !slices.Contains(rev2.SpanIDs, id) {
			t.Fatalf("inclusion violated: %s in rev1 but not rev2 (%v vs %v)",
				id, rev1Frozen.SpanIDs, rev2.SpanIDs)
		}
	}
	if !slices.Contains(rev2.SpanIDs, "root") {
		t.Fatal("rev2 must contain late root")
	}
	// 历史版本仍可取且不可变。
	got1, ok := a.GetRevision(tr, 1)
	if !ok || len(got1.SpanIDs) != 2 {
		t.Fatalf("rev1 history wrong: ok=%v ids=%v", ok, got1.SpanIDs)
	}
}

// 3. 精确重复静默忽略；负载冲突识别字段、先到先得、逐版累积。
func TestDuplicatesAndConflicts(t *testing.T) {
	a := New(Config{TimeoutNS: 100, SkewToleranceNS: 1000}, nil)
	const tr = "tr-dup"
	first := tspan(tr, "s1", "root", "api", "op", 10, 0, 10)
	dup := tspan(tr, "s1", "root", "api", "op", 15, 0, 10)
	bad := tspan(tr, "s1", "root", "api", "op2", 20, 0, 12) // operation + duration 冲突
	root := tspan(tr, "root", "", "gw", "entry", 25, -5, 30)

	r1 := mustIngest(t, a, first)
	if !r1[0].Accepted {
		t.Fatal("first should be accepted")
	}
	r2 := mustIngest(t, a, dup)
	if !r2[0].Duplicate || r2[0].Accepted || r2[0].Conflict != nil {
		t.Fatalf("exact dup wrong: %+v", r2[0])
	}
	r3 := mustIngest(t, a, bad)
	if r3[0].Conflict == nil || r3[0].Conflict.Kind != "payload-conflict" {
		t.Fatalf("conflict not detected: %+v", r3[0])
	}
	if !slices.Contains(r3[0].Conflict.Fields, "operation") ||
		!slices.Contains(r3[0].Conflict.Fields, "duration_nanos") {
		t.Fatalf("conflict fields wrong: %v", r3[0].Conflict.Fields)
	}
	mustIngest(t, a, root)
	mustAdvance(t, a, 200)
	rev, _, _ := a.GetTrace(tr)
	if len(rev.SpanIDs) != 2 {
		t.Fatalf("dup must not add span: %v", rev.SpanIDs)
	}
	if findSp(rev.Spans, "s1").Operation != "op" {
		t.Fatal("first-writer-wins violated")
	}
	if len(rev.Conflicts) != 1 {
		t.Fatalf("want 1 conflict, got %d", len(rev.Conflicts))
	}
	if !rev.Complete {
		t.Fatalf("conflict must not break structure: %v", rev.Reasons)
	}

	// 已关闭后再来一个不同字段的冲突：重开并在新修订中累积两条冲突。
	bad2 := tspan(tr, "s1", "root", "other-svc", "op", 300, 0, 10)
	mustIngest(t, a, bad2)
	rev2s := mustAdvance(t, a, 500)
	if len(rev2s) != 1 {
		t.Fatalf("conflicting late dup must reopen: %d", len(rev2s))
	}
	if len(rev2s[0].Conflicts) != 2 {
		t.Fatalf("conflicts must accumulate across revisions: %d", len(rev2s[0].Conflicts))
	}
	// span 集合包含关系仍成立。
	if len(rev2s[0].SpanIDs) != 2 {
		t.Fatalf("revision inclusion changed span set: %v", rev2s[0].SpanIDs)
	}
}

// 4. 跨服务时钟偏差：仅信息告警，结构完整，因果不靠时间戳。
func TestClockSkewAcrossServices(t *testing.T) {
	a := New(Config{TimeoutNS: 100, SkewToleranceNS: 1000}, nil)
	const tr = "tr-skew"
	// 父本地 10..30ms；子（另一服务）本地 5..40ms：两头越界。
	mustIngest(t, a,
		tspan(tr, "ch", "p", "svc-b", "child", 20, 5, 35), // 子先到
		tspan(tr, "p", "root", "svc-a", "parent", 10, 10, 20),
		tspan(tr, "root", "", "gw", "entry", 5, 0, 100),
	)
	mustAdvance(t, a, 200)
	rev, _, _ := a.GetTrace(tr)
	if !rev.Complete {
		t.Fatalf("skew must not affect completeness: %v", rev.Reasons)
	}
	var sawStart, sawEnd bool
	for _, e := range rev.Skew {
		if e.ChildSpanID != "ch" {
			continue
		}
		if e.Kind == "starts-before-parent" && e.DeltaNS < 0 {
			sawStart = true
		}
		if e.Kind == "ends-after-parent" && e.DeltaNS > 0 {
			sawEnd = true
		}
	}
	if !sawStart || !sawEnd {
		t.Fatalf("skew events missing: %+v", rev.Skew)
	}
	// 反例：把偏差挪回容忍带内，应无告警。
	a2 := New(Config{TimeoutNS: 100, SkewToleranceNS: 2_000_000}, nil)
	mustIngest(t, a2,
		tspan(tr, "ch2", "p2", "svc-b", "child", 20, 10, 10),
		tspan(tr, "p2", "root2", "svc-a", "parent", 10, 10, 20),
		tspan(tr, "root2", "", "gw", "entry", 5, 0, 100),
	)
	mustAdvance(t, a2, 200)
	rev2, _, _ := a2.GetTrace(tr)
	if len(rev2.Skew) != 0 {
		t.Fatalf("within tolerance must not alert: %+v", rev2.Skew)
	}
}

// 5. 父链循环：a→b→a，以及自环 a→a。
func TestParentCycle(t *testing.T) {
	a := New(Config{TimeoutNS: 100, SkewToleranceNS: 1000}, nil)
	const tr = "tr-cyc"
	mustIngest(t, a,
		tspan(tr, "a", "b", "x", "a", 10, 0, 10),
		tspan(tr, "b", "a", "x", "b", 20, 0, 10),
	)
	mustAdvance(t, a, 200)
	rev, _, _ := a.GetTrace(tr)
	if len(rev.Cycles) != 1 {
		t.Fatalf("want 1 cycle, got %d: %v", len(rev.Cycles), rev.Cycles)
	}
	if len(rev.Cycles[0]) != 2 || rev.Cycles[0][0] != "a" {
		t.Fatalf("cycle normalized wrong: %v", rev.Cycles[0])
	}
	if rev.Complete || !slices.Contains(rev.Reasons, ReasonCycle) {
		t.Fatalf("cycle trace incomplete+reason: %v", rev.Reasons)
	}

	// 自环。
	const tr2 = "tr-self"
	mustIngest(t, a, tspan(tr2, "s", "s", "x", "self", 10, 0, 10))
	mustAdvance(t, a, 400)
	rev2, _, _ := a.GetTrace(tr2)
	if len(rev2.Cycles) != 1 || rev2.Cycles[0][0] != "s" {
		t.Fatalf("self cycle wrong: %v", rev2.Cycles)
	}
}

// 6. 多根：信息性标记，不完整判定为 false（两个根没有缺失边，但不是单一根树）。
func TestMultipleRoots(t *testing.T) {
	a := New(Config{TimeoutNS: 100, SkewToleranceNS: 1000}, nil)
	const tr = "tr-multi"
	mustIngest(t, a,
		tspan(tr, "r1", "", "a", "r1", 10, 0, 10),
		tspan(tr, "r2", "", "b", "r2", 20, 0, 10),
	)
	mustAdvance(t, a, 200)
	rev, _, _ := a.GetTrace(tr)
	if rev.Complete {
		t.Fatal("two roots must not be complete")
	}
	if !slices.Contains(rev.Reasons, ReasonMultiRoot) {
		t.Fatalf("missing multi-root reason: %v", rev.Reasons)
	}
}

// 7. 超时语义只看逻辑水位：receive_ns=1000 与 receive_ns=10 行为同构，
// 且绝不读取壁钟（此处没有任何 sleep / time.Now）。
func TestTimeoutDrivenByWatermarkOnly(t *testing.T) {
	cfg := Config{TimeoutNS: 50, SkewToleranceNS: 0}
	a := New(cfg, nil)
	const tr = "tr-wm"
	mustIngest(t, a, tspan(tr, "c", "root", "a", "c", 1000, 0, 10))
	if got := mustAdvance(t, a, 1010); len(got) != 0 {
		t.Fatalf("idle 10 < 50 must not close")
	}
	if got := mustAdvance(t, a, 1049); len(got) != 0 {
		t.Fatalf("idle 49 < 50 must not close")
	}
	got := mustAdvance(t, a, 1050)
	if len(got) != 1 || got[0].Complete {
		t.Fatalf("idle 50 must close incomplete: %+v", got)
	}
	// 水位回退被忽略。
	if got := mustAdvance(t, a, 1); got != nil {
		t.Fatalf("watermark regress must be ignored, got %v", got)
	}
}

// 8. 非法报文整批拒绝，状态不变。
func TestValidationRejectsWholeBatch(t *testing.T) {
	a := New(Config{TimeoutNS: 100}, nil)
	good := tspan("tr", "ok", "", "a", "x", 1, 0, 1)
	bad := Span{TraceID: "tr", SpanID: "", Start: testT0}
	if _, _, err := a.IngestBatch([]Span{good, bad}, -1); err == nil {
		t.Fatal("expected validation error")
	}
	if summaries := a.ListTraces(); len(summaries) != 0 {
		t.Fatalf("state must be unchanged, got %d traces", len(summaries))
	}
}

// 9. 修订快照不可变：拿到 rev1 后继续摄入，rev1 的底层切片不被改写。
func TestRevisionImmutability(t *testing.T) {
	a := New(Config{TimeoutNS: 100}, nil)
	const tr = "tr-imm"
	mustIngest(t, a,
		tspan(tr, "c", "root", "a", "c", 10, 0, 10))
	mustAdvance(t, a, 200)
	rev1, _, _ := a.GetTrace(tr)
	snapshot := slices.Clone(rev1.SpanIDs)

	mustIngest(t, a, tspan(tr, "root", "", "gw", "r", 300, -10, 30))
	mustAdvance(t, a, 500)
	if len(rev1.SpanIDs) != len(snapshot) {
		t.Fatalf("rev1 mutated after rev2: %v vs %v", rev1.SpanIDs, snapshot)
	}
}

// 10. JSON 形态稳定：修订可序列化且关键字段存在。
func TestRevisionJSONShape(t *testing.T) {
	a := New(Config{TimeoutNS: 100}, nil)
	mustIngest(t, a, tspan("tr", "c", "root", "a", "c", 10, 0, 10))
	rev := mustAdvance(t, a, 200)[0]
	data, err := json.Marshal(rev)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"trace_id", "revision", "closed", "complete", "reasons",
		"span_ids", "missing_parents", "cycles", "clock_skew", "watermark_ns"} {
		if _, ok := m[k]; !ok {
			t.Fatalf("missing JSON field %q in %s", k, data)
		}
	}
}

func findSp(spans []Span, id string) Span {
	for _, s := range spans {
		if s.SpanID == id {
			return s
		}
	}
	return Span{}
}

// 11. 已关闭 trace 只收到精确重复：不重开、不产生新修订。
func TestExactDuplicateDoesNotReopenClosedTrace(t *testing.T) {
	a := New(Config{TimeoutNS: 100}, nil)
	const tr = "tr-reopen"
	first := tspan(tr, "root", "", "gw", "r", 10, 0, 10)
	mustIngest(t, a, first)
	rev1s := mustAdvance(t, a, 200)
	if len(rev1s) != 1 {
		t.Fatalf("want rev1, got %d", len(rev1s))
	}
	// 精确重复（receive_ns 不同不算冲突），同批次按当前水位扫描。
	res, emitted, err := a.IngestBatch([]Span{
		tspan(tr, "root", "", "gw", "r", 300, 0, 10),
	}, -1)
	if err != nil {
		t.Fatal(err)
	}
	if !res[0].Duplicate || res[0].Conflict != nil {
		t.Fatalf("want silent exact dup, got %+v", res[0])
	}
	if len(emitted) != 0 {
		t.Fatalf("exact dup must not create revision, got %d", len(emitted))
	}
	rev, _, live := a.GetTrace(tr)
	if live || rev.Revision != 1 {
		t.Fatalf("trace must stay closed at rev1, live=%v rev=%d", live, rev.Revision)
	}
}
