// Command demo 以合成数据演示并自检三类核心场景，不依赖壁钟：
//
//	A. 缺根 + 子先父后：超时输出不完整修订；根迟到后重开并生成包含前版的新修订；
//	B. 精确重复静默忽略、负载冲突识别并记录；
//	C. 跨服务时钟偏差告警（不影响结构）+ 父链循环检测。
//
// 退出码 0 表示全部内置断言通过，否则为 1。
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"slices"
	"time"

	"traceassembly/trace"
)

type check struct {
	Name   string `json:"name"`
	OK     bool   `json:"ok"`
	Detail string `json:"detail,omitempty"`
}

type scenario struct {
	Name      string           `json:"scenario"`
	Checks    []check          `json:"checks"`
	Revisions []trace.Revision `json:"revisions,omitempty"`
}

var t0 = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

func main() {
	scenarios := []scenario{scenarioA(), scenarioB(), scenarioC()}

	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	allOK := true
	for _, sc := range scenarios {
		for _, c := range sc.Checks {
			if !c.OK {
				allOK = false
			}
		}
		_ = enc.Encode(sc)
		fmt.Println()
	}
	if allOK {
		fmt.Println("demo: ALL CHECKS PASSED")
	} else {
		fmt.Println("demo: SOME CHECKS FAILED")
		os.Exit(1)
	}
}

func mk(spanID, parent, svc, op string, recvNS int64, startOffsetMS, durMS int64) trace.Span {
	return trace.Span{
		SpanID:        spanID,
		ParentSpanID:  parent,
		Service:       svc,
		Operation:     op,
		Start:         t0.Add(time.Duration(startOffsetMS) * time.Millisecond),
		DurationNanos: (time.Duration(durMS) * time.Millisecond).Nanoseconds(),
		ReceiveNS:     recvNS,
	}
}

func ingest(asm *trace.Assembler, spans ...trace.Span) {
	if _, _, err := asm.IngestBatch(spans, -1); err != nil {
		panic(err)
	}
}

func advance(asm *trace.Assembler, ns int64) []trace.Revision {
	revs, err := asm.AdvanceWatermark(ns)
	if err != nil {
		panic(err)
	}
	return revs
}

// scenarioA：缺根超时 + 迟到补全修订 + 逐版包含关系。
func scenarioA() scenario {
	asm := trace.New(trace.Config{TimeoutNS: 100, SkewToleranceNS: 1000}, nil)
	traceID := "tr-missing-root"

	g := mk("g1", "c1", "edge", "render", 10, 30, 5) // 孙子先到
	g.TraceID = traceID
	c := mk("c1", "root", "api", "call", 20, 10, 40) // 子先父后（父仍缺失）
	c.TraceID = traceID
	ingest(asm, g, c)

	rev1s := advance(asm, 200) // 空闲超时：10..20 最后到达，水位 200
	rev1, _, _ := asm.GetTrace(traceID)

	root := mk("root", "", "gateway", "entry", 300, 0, 100) // 根迟到
	root.TraceID = traceID
	ingest(asm, root)
	rev2s := advance(asm, 500)
	rev2, _, _ := asm.GetTrace(traceID)

	checks := []check{
		{"rev1 在水位推进时产生", len(rev1s) == 1, fmt.Sprintf("got=%d", len(rev1s))},
		{"rev1 已关闭", rev1.Closed, ""},
		{"rev1 标记不完整", !rev1.Complete, ""},
		{"rev1 带 timeout 原因", slices.Contains(rev1.Reasons, trace.ReasonTimeout), fmt.Sprint(rev1.Reasons)},
		{"rev1 缺根/缺父识别",
			slices.Contains(rev1.Reasons, "missing-root") &&
				slices.Contains(rev1.Reasons, trace.ReasonMissingParent) &&
				len(rev1.MissingParents) == 1,
			fmt.Sprint(rev1.Reasons)},
		{"rev1 不含根 span", !slices.Contains(rev1.SpanIDs, "root"), fmt.Sprint(rev1.SpanIDs)},
		{"迟到根触发新修订", len(rev2s) == 1, fmt.Sprintf("got=%d", len(rev2s))},
		{"rev2 完整", rev2.Complete, fmt.Sprint(rev2.Reasons)},
		{"rev2 编号=2 且修订自 rev1", rev2.Revision == 2 && rev2.RevisedOf == 1,
			fmt.Sprintf("rev=%d revisedOf=%d", rev2.Revision, rev2.RevisedOf)},
		{"rev2 带迟到修订原因", slices.Contains(rev2.Reasons, trace.ReasonRevised), ""},
		// 逐版包含：rev2 的 span 集合必须包含 rev1 的全部 span。
		{"包含关系: rev1 ⊆ rev2", containsAll(rev2.SpanIDs, rev1.SpanIDs),
			fmt.Sprintf("rev1=%v rev2=%v", rev1.SpanIDs, rev2.SpanIDs)},
		{"rev2 含迟到根", slices.Contains(rev2.SpanIDs, "root"), ""},
		{"结构按父子拼装（不看壁钟）: 单根", len(rev2.RootSpanIDs) == 1 && rev2.RootSpanIDs[0] == "root",
			fmt.Sprint(rev2.RootSpanIDs)},
	}
	return scenario{Name: "A. 缺根/子先父后 → 超时不完整 → 迟到补全新修订", Checks: checks,
		Revisions: []trace.Revision{rev1, rev2}}
}

// scenarioB：精确重复忽略、冲突识别。
func scenarioB() scenario {
	asm := trace.New(trace.Config{TimeoutNS: 100, SkewToleranceNS: 1000}, nil)
	traceID := "tr-dup"

	first := mk("s1", "root", "api", "op", 10, 0, 10)
	first.TraceID = traceID
	dup := mk("s1", "root", "api", "op", 15, 0, 10) // 精确重复（receive_ns 不同不算冲突）
	dup.TraceID = traceID
	conflict := mk("s1", "root", "api", "op-changed", 20, 0, 12) // operation/duration 冲突
	conflict.TraceID = traceID
	root := mk("root", "", "gw", "entry", 25, -5, 30)
	root.TraceID = traceID

	res1, _, _ := asm.IngestBatch([]trace.Span{first}, -1)
	res2, _, _ := asm.IngestBatch([]trace.Span{dup}, -1)
	res3, _, _ := asm.IngestBatch([]trace.Span{conflict}, -1)
	ingest(asm, root)
	advance(asm, 200)
	rev, _, _ := asm.GetTrace(traceID)

	checks := []check{
		{"首份被接受", res1[0].Accepted && !res1[0].Duplicate, ""},
		{"精确重复被识别且不接受", res2[0].Duplicate && !res2[0].Accepted && res2[0].Conflict == nil, ""},
		{"冲突被识别并给出字段", res3[0].Conflict != nil &&
			slices.Contains(res3[0].Conflict.Fields, "operation") &&
			slices.Contains(res3[0].Conflict.Fields, "duration_nanos"),
			fmt.Sprintf("%+v", res3[0].Conflict)},
		{"先到先得：规范 span 保留首份 operation",
			findSpan(rev.Spans, "s1").Operation == "op",
			""},
		{"冲突进入修订且仅记录一次", len(rev.Conflicts) == 1 &&
			rev.Conflicts[0].Kind == "payload-conflict",
			fmt.Sprintf("n=%d", len(rev.Conflicts))},
		{"重复不产生额外 span_id", len(rev.SpanIDs) == 2, fmt.Sprint(rev.SpanIDs)},
		{"图仍完整（冲突不影响结构）", rev.Complete, fmt.Sprint(rev.Reasons)},
	}
	return scenario{Name: "B. 精确重复忽略 / 负载冲突先到先得", Checks: checks,
		Revisions: []trace.Revision{rev}}
}

// scenarioC：跨服务时钟偏差 + 父链循环。
func scenarioC() scenario {
	asm := trace.New(trace.Config{TimeoutNS: 100, SkewToleranceNS: 1000}, nil)
	traceID := "tr-skew"

	// 父：本地时钟 10ms..30ms；子（另一服务）：本地 5ms..40ms —— 两端都越界。
	parent := mk("p", "root", "svc-a", "parent", 10, 10, 20)
	parent.TraceID = traceID
	child := mk("ch", "p", "svc-b", "child", 20, 5, 35)
	child.TraceID = traceID
	root := mk("root", "", "gw", "entry", 30, 0, 100)
	root.TraceID = traceID
	ingest(asm, child, parent, root) // 再次乱序：子先于父
	advance(asm, 200)
	rev, _, _ := asm.GetTrace(traceID)

	// 循环 trace：a.parent=b, b.parent=a。
	cycID := "tr-cycle"
	a := mk("a", "b", "svc-x", "loop-a", 10, 0, 10)
	a.TraceID = cycID
	b := mk("b", "a", "svc-x", "loop-b", 20, 0, 10)
	b.TraceID = cycID
	ingest(asm, a, b)
	advance(asm, 200)
	crev, _, _ := asm.GetTrace(cycID)

	checks := []check{
		{"时钟偏差检测到 starts-before-parent",
			hasSkew(rev.Skew, "ch", "starts-before-parent"), ""},
		{"时钟偏差检测到 ends-after-parent",
			hasSkew(rev.Skew, "ch", "ends-after-parent"),
			fmt.Sprintf("%+v", rev.Skew)},
		{"偏差仅信息性：图仍完整单根", rev.Complete && len(rev.RootSpanIDs) == 1,
			fmt.Sprint(rev.Reasons)},
		{"因果不依赖壁钟：根正确识别为 root", len(rev.RootSpanIDs) == 1 &&
			rev.RootSpanIDs[0] == "root", ""},
		{"父链循环被检测", len(crev.Cycles) == 1 &&
			slices.Contains(crev.Cycles[0], "a") && slices.Contains(crev.Cycles[0], "b"),
			fmt.Sprintf("%v", crev.Cycles)},
		{"循环 trace 标记不完整 + cycle 原因",
			!crev.Complete && slices.Contains(crev.Reasons, trace.ReasonCycle),
			fmt.Sprint(crev.Reasons)},
	}
	return scenario{Name: "C. 跨服务时钟偏差 / 父链循环", Checks: checks,
		Revisions: []trace.Revision{rev, crev}}
}

func containsAll(superset, subset []string) bool {
	for _, x := range subset {
		if !slices.Contains(superset, x) {
			return false
		}
	}
	return true
}

func findSpan(spans []trace.Span, id string) trace.Span {
	for _, s := range spans {
		if s.SpanID == id {
			return s
		}
	}
	return trace.Span{}
}

func hasSkew(events []trace.SkewEvent, child, kind string) bool {
	for _, e := range events {
		if e.ChildSpanID == child && e.Kind == kind {
			return true
		}
	}
	return false
}
