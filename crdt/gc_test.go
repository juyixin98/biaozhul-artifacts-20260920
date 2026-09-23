package crdt

import "testing"

func TestSafeReclaimAndGC(t *testing.T) {
	a := New()
	_ = a.AddWithTag("x", tag("a", 1))
	b := a.Clone()
	c := a.Clone()

	a.RemoveObserved("x")
	// b、c 尚未观察墓碑：不能安全回收。
	if a.SafeToReclaim(b, c) {
		t.Fatal("存在未观察墓碑的副本时不应可回收")
	}
	if a.SafeToReclaim(nil) {
		t.Fatal("nil 副本视为未知，不应可回收")
	}
	b.Merge(a)
	c.Merge(a)
	if !a.SafeToReclaim(b, c) {
		t.Fatal("全部副本观察墓碑后应可安全回收")
	}
	if n := a.ReclaimGC(); n != 1 {
		t.Fatalf("应回收 1 个墓碑标签，得到 %d", n)
	}
	if a.Contains("x") || len(a.A) != 0 || len(a.R) != 0 {
		t.Fatalf("回收后应为空状态: A=%v R=%v", a.A, a.R)
	}
	b.ReclaimGC()
	c.ReclaimGC()
	if !a.Equal(b) || !b.Equal(c) {
		t.Fatal("对称回收后各副本仍应相等")
	}
}

func TestUnsafeReclaimResurrects(t *testing.T) {
	a := New()
	_ = a.AddWithTag("x", tag("a", 1))
	laggard := a.Clone()
	a.RemoveObserved("x")
	a.ReclaimGC() // 未让 laggard 观察墓碑
	if a.Contains("x") {
		t.Fatal("回收后 x 不应存活")
	}
	a.Merge(laggard)
	if !a.Contains("x") {
		t.Fatal("滞后副本回归后 x 复活，是不稳定回收的预期反例")
	}
}

func TestEqualAndSignature(t *testing.T) {
	a := New()
	_ = a.AddWithTag("x", tag("a", 1))
	if a.Equal(nil) {
		t.Fatal("与 nil 不应相等")
	}
	b := a.Clone()
	if !a.Equal(b) || a.Signature() != b.Signature() {
		t.Fatal("克隆体应相等且指纹相同")
	}
	b.RemoveObserved("x")
	if a.Equal(b) {
		t.Fatal("有效值相同但墓碑不同 => 完整状态不等")
	}
	if a.Signature() == b.Signature() {
		t.Fatal("墓碑差异必须反映在指纹上")
	}
}

func TestExportSorted(t *testing.T) {
	s := New()
	_ = s.AddWithTag("x", tag("b", 2))
	_ = s.AddWithTag("x", tag("a", 1))
	a, _ := s.Export()
	tags := a["x"]
	if len(tags) != 2 || tags[0] != "a#1" || tags[1] != "b#2" {
		t.Fatalf("导出标签应升序: %v", tags)
	}
}
