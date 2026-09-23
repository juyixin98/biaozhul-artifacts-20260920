package analyzer

import (
	"strings"
	"testing"
)

// cpRow 描述手算关键路径上的一个期望归属项。
type cpRow struct {
	id   string
	attr int64
}

func attrMap(r *Report) map[string]int64 {
	m := map[string]int64{}
	for _, p := range r.CriticalPath {
		m[p.SpanID] = p.AttributedUs
	}
	return m
}

func diagKinds(r *Report) map[Severity][]string {
	m := map[Severity][]string{}
	for _, d := range r.Diagnostics {
		m[d.Code] = append(m[d.Code], d.Kind)
	}
	return m
}

func hasKind(list []string, kind string) bool {
	for _, k := range list {
		if k == kind {
			return true
		}
	}
	return false
}

// TestDemoHandComputed 用 README 中可手算的串并行 DAG 验证关键路径。
//
// 时间轴（半开区间）：
//
//	root    [0,300)
//	 write  [10,200)
//	  shard-a [50,130)  wal [60,90)
//	  shard-b [100,180) async
//	 gc      [210,260) async
//
// 手算扫描：
//
//	[0,10)    root  10
//	[10,50)   write 40
//	[50,60)   shard-a 10
//	[60,90)   wal 30
//	[90,130)  shard-a 40
//	[130,200) write 70        （shard-b 并行被吸收，不计）
//	[200,300) root 100       （gc 在 [210,260) 并行，root 没在等它，仍归 root）
//	合计 300，shard-b 与 gc 均不在关键路径上。
func TestDemoHandComputed(t *testing.T) {
	r := Analyze("demo-trace-1", DemoSpans("demo-trace-1"))
	if r.HasError {
		t.Fatalf("不应有错误级诊断: %+v", r.Diagnostics)
	}
	if r.RootDurationUs != 300 || r.CriticalPathDurationUs != 300 {
		t.Fatalf("根时长/关键路径时长 = %d/%d，期望 300/300", r.RootDurationUs, r.CriticalPathDurationUs)
	}
	want := map[string]int64{
		"root":    10 + 100, // [0,10)+[200,300)
		"write":   40 + 70,  // [10,50)+[130,200)
		"shard-a": 10 + 40,  // [50,60)+[90,130)
		"wal":     30,
	}
	got := attrMap(r)
	for id, v := range want {
		if got[id] != v {
			t.Errorf("%s 关键路径归属 = %d，期望 %d", id, got[id], v)
		}
	}
	if got["shard-b"] != 0 || got["gc"] != 0 {
		t.Errorf("并行任务 shard-b/gc 不应出现在关键路径上: %v", got)
	}
	// 路径顺序：按首次被归属的时间排列。
	var order []string
	for _, p := range r.CriticalPath {
		order = append(order, p.SpanID)
	}
	wantOrder := []string{"root", "write", "shard-a", "wal"}
	if strings.Join(order, ",") != strings.Join(wantOrder, ",") {
		t.Errorf("关键路径顺序 = %v，期望 %v", order, wantOrder)
	}
	// 并行余量：shard-b 在窗口内 80（全吸收），gc 50（全吸收）。
	if r.ParallelSlackUs != 130 {
		t.Errorf("parallel_slack = %d，期望 130", r.ParallelSlackUs)
	}
	// 自耗时：write=190-并集{[50,130),[100,180)}=190-130=60；
	// shard-a=80-30=50；wal=30；root=300-并集{[10,200),[210,260)}=300-240=60。
	st := r.SelfTimes
	if st["write"] != 60 || st["shard-a"] != 50 || st["wal"] != 30 || st["root"] != 60 {
		t.Errorf("自耗时 root/write/shard-a/wal = %d/%d/%d/%d，期望 60/60/50/30",
			st["root"], st["write"], st["shard-a"], st["wal"])
	}
	// 所有 span 原始耗时之和 = 300+190+80+30+80+50 = 730，远大于墙上 300（并行重复计数）。
	if r.SumSpanDurationUs != 730 {
		t.Errorf("span 耗时之和 = %d，期望 730", r.SumSpanDurationUs)
	}
}

// TestOverlappingSyncSiblings 覆盖“重叠区间”：
// 两个同步兄弟 [10,60) 与 [40,90) 重叠，触发 OVERLAPPING_SYNC_SIBLINGS 警告，
// 仍给出结果（422 之外的 warning），重叠段 [40,60) 按较晚结束者 b 归属。
func TestOverlappingSyncSiblings(t *testing.T) {
	r := Analyze("t", OverlappingSpans("t"))
	if r.HasError {
		t.Fatalf("重叠只是警告，不应是 error: %+v", r.Diagnostics)
	}
	d := diagKinds(r)
	if !hasKind(d[SevWarning], "OVERLAPPING_SYNC_SIBLINGS") {
		t.Fatalf("期望 OVERLAPPING_SYNC_SIBLINGS 警告，实际: %+v", r.Diagnostics)
	}
	got := attrMap(r)
	// [0,10) r=10；[10,40) a=30；[40,90) b=50（结束更晚，重叠段归 b）；[90,100) r=10。
	if got["r"] != 20 || got["a"] != 30 || got["b"] != 50 {
		t.Errorf("归属 r/a/b = %d/%d/%d，期望 20/30/50", got["r"], got["a"], got["b"])
	}
	if r.CriticalPathDurationUs != 100 {
		t.Errorf("关键路径时长 = %d，期望 100", r.CriticalPathDurationUs)
	}
}

// TestCycle 覆盖循环输入：c->a...->c，返回 error 级诊断且不产出路径。
func TestCycle(t *testing.T) {
	r := Analyze("t", CycleSpans("t"))
	if !r.HasError {
		t.Fatal("循环必须产生 error 级诊断")
	}
	d := diagKinds(r)
	if !hasKind(d[SevError], "CYCLE_DETECTED") {
		t.Fatalf("期望 CYCLE_DETECTED，实际: %+v", r.Diagnostics)
	}
	if len(r.CriticalPath) != 0 || r.CriticalPathDurationUs != 0 {
		t.Errorf("存在循环时不应计算关键路径: %+v", r.CriticalPath)
	}
	var foundCycleText bool
	for _, g := range r.Diagnostics {
		if g.Kind == "CYCLE_DETECTED" && strings.Contains(g.Message, "->") {
			foundCycleText = true
		}
	}
	if !foundCycleText {
		t.Error("循环诊断应给出具体环路径")
	}
}

// TestMissingParent 覆盖缺 span（孤儿提升为根）与孩子越过父区间两种诊断。
func TestMissingParent(t *testing.T) {
	r := Analyze("t", MissingParentSpans("t"))
	if r.HasError {
		t.Fatalf("缺父/越界是警告不是错误: %+v", r.Diagnostics)
	}
	d := diagKinds(r)
	if !hasKind(d[SevWarning], "ORPHAN_SPAN") {
		t.Error("期望 ORPHAN_SPAN 警告")
	}
	if !hasKind(d[SevWarning], "MULTIPLE_ROOTS") {
		t.Error("期望 MULTIPLE_ROOTS 警告")
	}
	if !hasKind(d[SevWarning], "CHILD_OUTSIDE_PARENT") {
		t.Error("期望 CHILD_OUTSIDE_PARENT 警告")
	}
	if r.RootID != "r" {
		t.Errorf("主根应为最早开始的 r，实际 %s", r.RootID)
	}
	// late[80,140) 是同步孩子，观察到的 trace 窗口被真实延伸到 140：
	// [0,80) root=80；[80,140) late=60（越过父结束的部分已由 CHILD_OUTSIDE_PARENT 告警）。
	if r.CriticalPathDurationUs != 140 {
		t.Errorf("关键路径时长 = %d，期望 140（同步孩子越界顶长观察窗口）", r.CriticalPathDurationUs)
	}
	got := attrMap(r)
	if got["r"] != 80 || got["late"] != 60 {
		t.Errorf("归属 root/late = %d/%d，期望 80/60", got["r"], got["late"])
	}
}

// TestSelfTimeOverlapUnion 用两个互相重叠的孩子验证自耗时的并集去重：
// naive 求和会扣 50+50=100 得到 0；并集 [10,90) 长 80，自耗时应为 20。
func TestSelfTimeOverlapUnion(t *testing.T) {
	r := Analyze("t", NestedOverlapSpans("t"))
	if r.SelfTimes["r"] != 20 {
		t.Errorf("自耗时 = %d，期望 20（重叠区间只扣一次）", r.SelfTimes["r"])
	}
	// 两个 async 孩子都在 root 窗口内并行完成，不顶关键路径：
	// [0,10)+[90,100) root=20；[10,90) root 在等吗？async 不等——但没有同步孩子，
	// 该段归 root（同步集合只剩 root）。
	got := attrMap(r)
	if got["r"] != 100 {
		t.Errorf("关键路径应全归 root=100，实际 %v", got)
	}
	if r.CriticalPathDurationUs != 100 {
		t.Errorf("关键路径时长 = %d，期望 100", r.CriticalPathDurationUs)
	}
	if r.ParallelSlackUs != 80 {
		t.Errorf("并行余量 = %d，期望 80（x:50 + y:50 - 重叠30 = 并集80）", r.ParallelSlackUs)
	}
}

// TestAsyncTailExtendsTrace 验证“越过根结束的异步尾部”会顶长 trace 上关键路径。
func TestAsyncTailExtendsTrace(t *testing.T) {
	spans := []Span{
		{SpanID: "r", Name: "root", StartUs: 0, EndUs: 100},
		{SpanID: "work", ParentID: "r", Name: "sync-work", StartUs: 0, EndUs: 60},
		{SpanID: "bg", ParentID: "r", Name: "leaked-bg", StartUs: 70, EndUs: 140, Async: true},
	}
	r := Analyze("t", spans)
	if r.HasError {
		t.Fatalf("不应有 error: %+v", r.Diagnostics)
	}
	d := diagKinds(r)
	if !hasKind(d[SevWarning], "ASYNC_TAIL_EXTENDS_TRACE") {
		t.Fatal("期望 ASYNC_TAIL_EXTENDS_TRACE 警告")
	}
	got := attrMap(r)
	// [0,60) work=60；[60,100) root=40（bg 并行但 root 未等）；[100,140) 无同步 span，归 bg=40。
	if got["work"] != 60 || got["r"] != 40 || got["bg"] != 40 {
		t.Errorf("归属 work/r/bg = %d/%d/%d，期望 60/40/40", got["work"], got["r"], got["bg"])
	}
	if r.CriticalPathDurationUs != 140 {
		t.Errorf("关键路径时长 = %d，期望 140（异步尾部真实延长 trace）", r.CriticalPathDurationUs)
	}
	if r.RootDurationUs != 100 {
		t.Errorf("根时长保持 %d，期望 100", r.RootDurationUs)
	}
}

// TestStructuralErrors 覆盖负时长、重复 ID、空 trace。
func TestStructuralErrors(t *testing.T) {
	t.Run("negative duration", func(t *testing.T) {
		r := Analyze("t", []Span{{SpanID: "a", Name: "a", StartUs: 10, EndUs: 5}})
		if !r.HasError || !hasKind(diagKinds(r)[SevError], "NEGATIVE_DURATION") {
			t.Fatalf("期望 NEGATIVE_DURATION: %+v", r.Diagnostics)
		}
	})
	t.Run("duplicate id", func(t *testing.T) {
		r := Analyze("t", []Span{
			{SpanID: "a", Name: "a", StartUs: 0, EndUs: 10},
			{SpanID: "a", Name: "a2", StartUs: 0, EndUs: 20},
		})
		if !r.HasError || !hasKind(diagKinds(r)[SevError], "DUPLICATE_SPAN_ID") {
			t.Fatalf("期望 DUPLICATE_SPAN_ID: %+v", r.Diagnostics)
		}
	})
	t.Run("empty trace", func(t *testing.T) {
		r := Analyze("t", nil)
		if !r.HasError || !hasKind(diagKinds(r)[SevError], "EMPTY_TRACE") {
			t.Fatalf("期望 EMPTY_TRACE: %+v", r.Diagnostics)
		}
	})
}

// TestParallelChainIsNotSum 专门防止“所有子耗时之和”的错误算法：
// 三个 async 子任务完全并行重叠，关键路径只计墙上长度，不计时长之和。
func TestParallelChainIsNotSum(t *testing.T) {
	spans := []Span{
		{SpanID: "r", Name: "root", StartUs: 0, EndUs: 100},
		{SpanID: "p1", ParentID: "r", Name: "p1", StartUs: 10, EndUs: 90, Async: true},
		{SpanID: "p2", ParentID: "r", Name: "p2", StartUs: 10, EndUs: 90, Async: true},
		{SpanID: "p3", ParentID: "r", Name: "p3", StartUs: 10, EndUs: 90, Async: true},
	}
	r := Analyze("t", spans)
	if r.SumSpanDurationUs != 340 {
		t.Errorf("原始耗时之和 = %d，期望 340", r.SumSpanDurationUs)
	}
	if r.CriticalPathDurationUs != 100 {
		t.Errorf("关键路径必须是墙上 100 而非求和，实际 %d", r.CriticalPathDurationUs)
	}
	if got := attrMap(r)["r"]; got != 100 {
		t.Errorf("并行任务在根窗口内全部吸收，root 应归属 100，实际 %d", got)
	}
}

// TestSynthDeterministic 多层合成 trace 冒烟：关键路径覆盖整个根窗口且无错误。
func TestSynthDeterministic(t *testing.T) {
	spans := RandomishSpans("synth", 4)
	r := Analyze("synth", spans)
	if r.HasError {
		t.Fatalf("合成 trace 不应有错误: %+v", r.Diagnostics)
	}
	if r.CriticalPathDurationUs != r.RootDurationUs {
		t.Errorf("无异步尾部越界时关键路径 %d 应等于根时长 %d",
			r.CriticalPathDurationUs, r.RootDurationUs)
	}
}

// TestUnionLength 并集算法单测：相接、包含、完全重叠。
func TestUnionLength(t *testing.T) {
	cases := []struct {
		name string
		ivs  [][2]int64
		want int64
	}{
		{"empty", nil, 0},
		{"adjacent", [][2]int64{{0, 10}, {10, 20}}, 20},
		{"overlap", [][2]int64{{0, 10}, {5, 15}}, 15},
		{"contained", [][2]int64{{0, 100}, {10, 20}, {30, 40}}, 100},
		{"disjoint", [][2]int64{{0, 10}, {20, 30}}, 20},
		{"three-chain", [][2]int64{{0, 10}, {5, 30}, {25, 40}}, 40},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := unionLength(c.ivs); got != c.want {
				t.Errorf("unionLength = %d，期望 %d", got, c.want)
			}
		})
	}
}
