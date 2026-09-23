package crdt

import (
	"testing"
)

func tag(origin string, counter uint64) UniqueTag {
	return UniqueTag{Origin: origin, Counter: counter}
}

func TestAddContainsValues(t *testing.T) {
	s := New()
	if _, err := s.Add("x", "r1", 1); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Add("y", "r1", 2); err != nil {
		t.Fatal(err)
	}
	if !s.Contains("x") || !s.Contains("y") || s.Contains("z") {
		t.Fatalf("Contains 异常: %v", s.Values())
	}
	if got := s.Values(); len(got) != 2 || got[0] != "x" || got[1] != "y" {
		t.Fatalf("Values 应为 [x y]，实际 %v", got)
	}
}

func TestUniqueTagRejected(t *testing.T) {
	s := New()
	if err := s.AddWithTag("x", tag("r1", 1)); err != nil {
		t.Fatal(err)
	}
	// 同标签加到另一个值必须被拒。
	if err := s.AddWithTag("z", tag("r1", 1)); err == nil {
		t.Fatal("重复标签复用到 z 应报错")
	}
	// 标签进入墓碑后同样不可复用。
	if _, ok := s.RemoveObserved("x"); !ok {
		t.Fatal("删除应成功")
	}
	if err := s.AddWithTag("x", tag("r1", 1)); err == nil {
		t.Fatal("已墓碑化标签复用应报错")
	}
}

func TestRemoveObservedSemantics(t *testing.T) {
	// r1 有 x；r2 独立也有 x（并发添加，不同标签）。
	r1 := New()
	_ = r1.AddWithTag("x", tag("r1", 1))
	r2 := New()
	_ = r2.AddWithTag("x", tag("r2", 1))

	// r1 删除自己观察到的 x，只墓碑化 r1#1。
	removed, ok := r1.RemoveObserved("x")
	if !ok || len(removed) != 1 || removed[0] != tag("r1", 1) {
		t.Fatalf("删除集合错误: %v ok=%v", removed, ok)
	}
	if r1.Contains("x") {
		t.Fatal("r1 删除后 x 不应存活")
	}
	// 观察到并发新增后，x 重新存活。
	r1.Merge(r2)
	if !r1.Contains("x") {
		t.Fatal("并发新增标签到达后 x 必须存活")
	}
	if got := r1.LiveTags("x"); len(got) != 1 || got[0] != tag("r2", 1) {
		t.Fatalf("存活标签应只剩 r2#1: %v", got)
	}
	// 删除不存在 / 已删除元素返回 false。
	if _, ok := r1.RemoveObserved("nope"); ok {
		t.Fatal("删除不存在元素应返回 false")
	}
	r1.RemoveObserved("x")
	if _, ok := r1.RemoveObserved("x"); ok {
		t.Fatal("再次删除应返回 false（无存活标签）")
	}
}

func TestMergeIdempotent(t *testing.T) {
	a := New()
	_ = a.AddWithTag("x", tag("a", 1))
	b := a.Clone()
	b.Merge(a.Clone())
	b.Merge(a.Clone())
	if !b.Equal(a) {
		t.Fatal("重复合并应幂等")
	}
}

func TestMergeCommutativeAssociative(t *testing.T) {
	a := New()
	_ = a.AddWithTag("x", tag("a", 1))
	b := New()
	_ = b.AddWithTag("y", tag("b", 1))
	b.Merge(a)
	b.RemoveObserved("x")
	c := New()
	_ = c.AddWithTag("x", tag("c", 1)) // 与 b 的删除并发

	ab_then_c := a.Clone()
	ab_then_c.Merge(b)
	ab_then_c.Merge(c)

	cb_then_a := c.Clone()
	cb_then_a.Merge(b)
	cb_then_a.Merge(a)

	if !ab_then_c.Equal(cb_then_a) {
		t.Fatal("不同合并顺序应得到相同完整状态")
	}
	if got := ab_then_c.Values(); len(got) != 2 || got[0] != "x" || got[1] != "y" {
		t.Fatalf("合并后应为 {x（并发新增存活）, y}，得到 %v", got)
	}
}

func TestCloneIndependent(t *testing.T) {
	a := New()
	_ = a.AddWithTag("x", tag("a", 1))
	b := a.Clone()
	b.RemoveObserved("x")
	if !a.Contains("x") {
		t.Fatal("Clone 必须是深拷贝")
	}
}

func TestEmptyValueRejected(t *testing.T) {
	if err := New().AddWithTag("", tag("a", 1)); err == nil {
		t.Fatal("空元素应报错")
	}
}
