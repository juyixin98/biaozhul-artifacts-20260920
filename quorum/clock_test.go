package quorum

import "testing"

func TestClockRelations(t *testing.T) {
	a := Clock{"n1": 1}
	b := Clock{"n1": 2}
	c := Clock{"n2": 1}
	ab := Clock{"n1": 2, "n2": 1}

	if !ClockLess(a, b) {
		t.Fatalf("期望 {n1:1} -> {n1:2}")
	}
	if ClockLess(b, a) {
		t.Fatalf("反向不应成立")
	}
	if !ClockLess(a, ab) {
		t.Fatalf("期望 {n1:1} -> {n1:2,n2:1}")
	}
	if !ClockLess(c, ab) {
		t.Fatalf("期望 {n2:1} -> {n1:2,n2:1}")
	}
	if !ClockConcurrent(b, c) {
		t.Fatalf("{n1:2} 与 {n2:1} 必须并发（不可比较）")
	}
	if ClockConcurrent(a, b) {
		t.Fatalf("因果有序的钟不应被判为并发")
	}
	if !ClockEqual(MergeClock(b, c), ab) {
		t.Fatalf("合并结果应为 %s，得到 %s", ab, MergeClock(b, c))
	}
	// 缺失分量按 0：空钟先于一切非空钟。
	if !ClockLess(Clock{}, a) {
		t.Fatalf("空钟应严格先于非空钟")
	}
	if ClockConcurrent(Clock{}, Clock{}) {
		t.Fatalf("两个空钟相等，不是并发")
	}
}

func TestClockStringStable(t *testing.T) {
	got := Clock{"n3": 1, "n1": 2, "n2": 0}.String()
	if want := "{n1:2,n3:1}"; got != want {
		t.Fatalf("稳定渲染失败: got %s want %s", got, want)
	}
}

func TestMaximalVersionsPrunesDominated(t *testing.T) {
	v1 := Version{ID: "v1", Value: "a", Clock: Clock{"n1": 1}}
	v2 := Version{ID: "v2", Value: "b", Clock: Clock{"n1": 2}}
	v3 := Version{ID: "v3", Value: "c", Clock: Clock{"n2": 1}} // 与 v2 并发

	got := maximalVersions([]Version{v1, v2, v3, v1})
	if len(got) != 2 {
		t.Fatalf("v1 被 v2 支配应剪枝、v1 重复应去重，期望剩 2 个，得到 %d: %+v", len(got), got)
	}
	ids := map[string]bool{}
	for _, v := range got {
		ids[v.ID] = true
	}
	if !ids["v2"] || !ids["v3"] {
		t.Fatalf("应保留 v2/v3 兄弟版本，得到 %v", ids)
	}
}
