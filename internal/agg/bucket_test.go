package agg

import (
	"math"
	"testing"
)

func TestAddBasic(t *testing.T) {
	var b Bucket
	for _, v := range []float64{3, 1, 4, 1, 5, 9, -2} {
		b.Add(v)
	}
	if b.Count != 7 {
		t.Fatalf("count=%d 期望 7", b.Count)
	}
	// 3+1+4+1+5+9-2 = 21
	if math.Abs(b.Sum-21) > 1e-9 {
		t.Fatalf("sum=%v 期望 21", b.Sum)
	}
	if b.Min != -2 {
		t.Fatalf("min=%v 期望 -2", b.Min)
	}
	if b.Max != 9 {
		t.Fatalf("max=%v 期望 9", b.Max)
	}
	if math.Abs(b.Avg()-3) > 1e-9 {
		t.Fatalf("avg=%v 期望 3", b.Avg())
	}
}

func TestEmptyBucket(t *testing.T) {
	var b Bucket
	if b.Count != 0 || !math.IsNaN(b.Avg()) {
		t.Fatalf("空桶状态错误: %+v", b)
	}
	b.Merge(Bucket{}) // 合并空桶不应改变状态
	if b.Count != 0 {
		t.Fatalf("合并空桶后 count=%d", b.Count)
	}
}

// TestMergeNotAverageOfAverages 是本项目的核心正确性证明：
// 两个分钟桶样本数悬殊时，小时均值绝不等于分钟均值的简单平均，
// 但 Merge（sum/count 相加）的结果与从原始样本重算一致。
func TestMergeNotAverageOfAverages(t *testing.T) {
	// 分钟桶 A：1 个样本，值 100。
	var a Bucket
	a.Add(100)
	// 分钟桶 B：99 个样本，值全为 2。
	var b2 Bucket
	for i := 0; i < 99; i++ {
		b2.Add(2)
	}

	// 错误做法：均值的均值 = (100+2)/2 = 51，完全失真。
	naive := (a.Avg() + b2.Avg()) / 2
	if math.Abs(naive-51) > 1e-9 {
		t.Fatalf("naive 前置计算错误: %v", naive)
	}

	// 正确做法：合并充分统计量。
	var hour Bucket
	hour.Merge(a)
	hour.Merge(b2)
	if hour.Count != 100 {
		t.Fatalf("count=%d 期望 100", hour.Count)
	}
	wantSum := 100.0 + 99*2
	if math.Abs(hour.Sum-wantSum) > 1e-9 {
		t.Fatalf("sum=%v 期望 %v", hour.Sum, wantSum)
	}
	wantAvg := wantSum / 100 // 2.98
	if math.Abs(hour.Avg()-wantAvg) > 1e-9 {
		t.Fatalf("正确均值=%v 期望 %v", hour.Avg(), wantAvg)
	}
	if math.Abs(hour.Avg()-naive) < 0.5 {
		t.Fatalf("正确均值 %v 不应接近均值再平均 %v", hour.Avg(), naive)
	}

	// 与原始重算逐样本 Add 的结果完全一致。
	var recompute Bucket
	recompute.Add(100)
	for i := 0; i < 99; i++ {
		recompute.Add(2)
	}
	if !hour.AlmostEqual(recompute, 1e-12) {
		t.Fatalf("Merge 结果 %+v 与原始重算 %+v 不一致", hour, recompute)
	}
}

func TestMergeMinMax(t *testing.T) {
	var a, b, c Bucket
	a.Add(5)
	b.Add(-3)
	b.Add(20)
	c.Add(7)
	var merged Bucket
	merged.Merge(a)
	merged.Merge(Bucket{}) // 空桶不影响
	merged.Merge(b)
	merged.Merge(c)
	if merged.Min != -3 || merged.Max != 20 || merged.Count != 4 {
		t.Fatalf("merge 结果错误: %+v", merged)
	}
}
