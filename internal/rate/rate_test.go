package rate

import (
	"encoding/json"
	"math"
	"testing"

	"counterreset/internal/series"
)

// 手算基准序列（README §手算验证 逐项对照）：
//
//	t(秒)   0   10   20   30   50   60   70   80   90  100
//	v      0   10   20   20   30   40   45   60   10   15
//	                                    重置在 90 前
const baseMs = int64(1700000000000)

func handSamples() []series.Sample {
	return []series.Sample{
		{TimestampMs: baseMs + 0, Value: 0},
		{TimestampMs: baseMs + 10000, Value: 10},
		{TimestampMs: baseMs + 20000, Value: 20},
		{TimestampMs: baseMs + 30000, Value: 20},
		{TimestampMs: baseMs + 50000, Value: 30},
		{TimestampMs: baseMs + 60000, Value: 40},
		{TimestampMs: baseMs + 70000, Value: 45},
		{TimestampMs: baseMs + 80000, Value: 60},
		{TimestampMs: baseMs + 90000, Value: 10},
		{TimestampMs: baseMs + 100000, Value: 15},
	}
}

const eps = 1e-9

func approxEq(t *testing.T, name string, got, want float64) {
	t.Helper()
	if math.Abs(got-want) > eps {
		t.Fatalf("%s = %v, 期望 %v", name, got, want)
	}
}

func TestComputeHandCheckedFullWindow(t *testing.T) {
	// 完整窗口 [0,100]，观测正好覆盖窗口，三种策略结果应当一致。
	for _, extrap := range []Extrapolation{ExtrapNone, ExtrapLinear, ExtrapClamped} {
		t.Run(string(extrap), func(t *testing.T) {
			r, err := Compute(handSamples(), baseMs, baseMs+100000, extrap, 0)
			if err != nil {
				t.Fatal(err)
			}
			if r.SampleCount != 10 {
				t.Fatalf("样本数 = %d", r.SampleCount)
			}
			// 手算：(10+10+0)+(10+10+5+15)+(10+5) = 75
			approxEq(t, "point", r.Increase.Point, 75)
			approxEq(t, "lower", r.Increase.Lower, 75)
			if r.Increase.Upper != nil {
				t.Fatalf("无容量时 upper 应为 null, 实际 %v", *r.Increase.Upper)
			}
			approxEq(t, "rate", r.RatePerSecond.Point, 0.75)
			approxEq(t, "factor", r.ExtrapolationFactor, 1)
			approxEq(t, "coverage", r.ObservedCoverage, 1)

			if len(r.Resets) != 1 {
				t.Fatalf("重置次数 = %d, 期望 1: %+v", len(r.Resets), r.Resets)
			}
			rz := r.Resets[0]
			if rz.BeforeTsMs != baseMs+80000 || rz.BeforeVal != 60 || rz.AfterVal != 10 {
				t.Fatalf("重置明细异常: %+v", rz)
			}
			if len(r.Gaps) != 1 {
				t.Fatalf("缺样区间数 = %d, 期望 1", len(r.Gaps))
			}
			g := r.Gaps[0]
			if g.FromTsMs != baseMs+30000 || g.DeltaMs != 20000 || g.GapRatio != 2 {
				t.Fatalf("缺样明细异常: %+v", g)
			}
			// 重置区间明细：v1=60→v2=10, 下界 = 10。
			var found bool
			for _, iv := range r.Intervals {
				if iv.Reset {
					found = true
					approxEq(t, "重置区间增量下界", iv.IncreaseLb, 10)
					if iv.IncreaseUb != nil {
						t.Fatal("无容量时区间上界应为 null")
					}
				}
			}
			if !found {
				t.Fatal("区间明细中找不到重置区间")
			}
		})
	}
}

func TestComputeConditionalUpperWithCapacity(t *testing.T) {
	r, err := Compute(handSamples(), baseMs, baseMs+100000, ExtrapNone, 100)
	if err != nil {
		t.Fatal(err)
	}
	// 手算：9 个区间 Σ(C-v1+v2) = 9*100 - Σv1(235) + Σv2(250) = 915
	if r.Increase.Upper == nil {
		t.Fatal("给定容量后 upper 不应为 null")
	}
	approxEq(t, "条件上界", *r.Increase.Upper, 915)
	if r.RatePerSecond.Upper == nil {
		t.Fatal("速率条件上界不应为 null")
	}
	approxEq(t, "速率条件上界", *r.RatePerSecond.Upper, 9.15)
	if r.Capacity == nil || *r.Capacity != 100 {
		t.Fatalf("capacity 回显异常: %v", r.Capacity)
	}
}

func TestComputeNoneStrategyNoExtrapolation(t *testing.T) {
	// 窗口 [0,200]：仅前 100s 有样本。none 不外推，point 仍为 75，coverage=0.5。
	r, err := Compute(handSamples(), baseMs, baseMs+200000, ExtrapNone, 0)
	if err != nil {
		t.Fatal(err)
	}
	approxEq(t, "point", r.Increase.Point, 75)
	approxEq(t, "factor", r.ExtrapolationFactor, 1)
	approxEq(t, "coverage", r.ObservedCoverage, 0.5)
	// 速率分母是整个窗口：75 / 200s
	approxEq(t, "rate", r.RatePerSecond.Point, 0.375)
}

func TestComputeLinearExtrapolation(t *testing.T) {
	// [0,200] linear：factor=2，point=150，rate=0.75。
	r, err := Compute(handSamples(), baseMs, baseMs+200000, ExtrapLinear, 0)
	if err != nil {
		t.Fatal(err)
	}
	approxEq(t, "point", r.Increase.Point, 150)
	approxEq(t, "factor", r.ExtrapolationFactor, 2)
	approxEq(t, "rate", r.RatePerSecond.Point, 0.75)
}

func TestComputeClampedExtrapolation(t *testing.T) {
	// [0,200] clamped：平均间隔 100/9≈11.111s，阈值≈12.222s；
	// 左侧边界距离 0（补 0），右侧距离 100 ≥ 阈值，只补 avg/2≈5.556s，
	// factor = (100+5.556)/100 = 19/18；point = 75*19/18 ≈ 79.167。
	r, err := Compute(handSamples(), baseMs, baseMs+200000, ExtrapClamped, 0)
	if err != nil {
		t.Fatal(err)
	}
	wantFactor := 19.0 / 18.0
	approxEq(t, "factor", r.ExtrapolationFactor, wantFactor)
	approxEq(t, "point", r.Increase.Point, 75*wantFactor)
	approxEq(t, "rate", r.RatePerSecond.Point, 75*wantFactor/200)
}

func TestComputePartialWindowNoClamp(t *testing.T) {
	// 窗口 [10,90]：选中样本 v10..v90（首尾正好在窗口边界），
	// 两侧外推距离均为 0，clamped 与 none 一致，factor=1。
	// 原始下界 = (20-10)+(20-20)+(30-20)+(40-30)+(45-40)+(60-45)+(10) = 60。
	r, err := Compute(handSamples(), baseMs+10000, baseMs+90000, ExtrapClamped, 0)
	if err != nil {
		t.Fatal(err)
	}
	approxEq(t, "raw", r.Raw.Point, 60)
	approxEq(t, "factor", r.ExtrapolationFactor, 1)
	approxEq(t, "point", r.Increase.Point, 60)
	approxEq(t, "rate", r.RatePerSecond.Point, 0.75)

	// 窗口 [5,95]：两端各 5s < 阈值 1.1*avg，按实际距离外推，
	// factor = 90/80 = 9/8；raw=60，point=67.5，rate=67.5/90=0.75。
	r2, err := Compute(handSamples(), baseMs+5000, baseMs+95000, ExtrapClamped, 0)
	if err != nil {
		t.Fatal(err)
	}
	approxEq(t, "raw2", r2.Raw.Point, 60)
	approxEq(t, "factor2", r2.ExtrapolationFactor, 9.0/8.0)
	approxEq(t, "point2", r2.Increase.Point, 67.5)
	approxEq(t, "rate2", r2.RatePerSecond.Point, 0.75)
}

func TestComputeClampFarEdgeRule(t *testing.T) {
	// 只有 t=0,v0 与 t=5,v10 两个样本，窗口 [0,100]：
	// avg=5s，阈值 5.5s；左距 0、右距 95 ≥ 阈值 → 右端只补 avg/2=2.5s，
	// factor = (5+2.5)/5 = 1.5，point = 15。
	s := []series.Sample{
		{TimestampMs: baseMs, Value: 0},
		{TimestampMs: baseMs + 5000, Value: 10},
	}
	r, err := Compute(s, baseMs, baseMs+100000, ExtrapClamped, 0)
	if err != nil {
		t.Fatal(err)
	}
	approxEq(t, "远端截断 factor", r.ExtrapolationFactor, 1.5)
	approxEq(t, "point", r.Increase.Point, 15)
	approxEq(t, "rate", r.RatePerSecond.Point, 0.15)
}

func TestComputeMultipleResets(t *testing.T) {
	// 多次重置序列：0→10→2(重置)→8→1(重置)→5
	s := []series.Sample{
		{TimestampMs: baseMs, Value: 0},
		{TimestampMs: baseMs + 10000, Value: 10},
		{TimestampMs: baseMs + 20000, Value: 2},
		{TimestampMs: baseMs + 30000, Value: 8},
		{TimestampMs: baseMs + 40000, Value: 1},
		{TimestampMs: baseMs + 50000, Value: 5},
	}
	r, err := Compute(s, baseMs, baseMs+50000, ExtrapNone, 0)
	if err != nil {
		t.Fatal(err)
	}
	// 10 + 2 + 6 + 1 + 4 = 23
	approxEq(t, "point", r.Increase.Point, 23)
	if len(r.Resets) != 2 {
		t.Fatalf("重置次数 = %d, 期望 2", len(r.Resets))
	}
	// C=100 条件上界：5*100 - (0+10+2+8+1) + (10+2+8+1+5)
	// = 500 - 21 + 26 = 505
	r2, err := Compute(s, baseMs, baseMs+50000, ExtrapNone, 100)
	if err != nil {
		t.Fatal(err)
	}
	approxEq(t, "条件上界", *r2.Increase.Upper, 505)
}

func TestComputeFlatCounterNoReset(t *testing.T) {
	// 值持平不是重置：20→20 增量为 0。
	s := []series.Sample{
		{TimestampMs: baseMs, Value: 20},
		{TimestampMs: baseMs + 10000, Value: 20},
		{TimestampMs: baseMs + 20000, Value: 20},
	}
	r, err := Compute(s, baseMs, baseMs+20000, ExtrapNone, 0)
	if err != nil {
		t.Fatal(err)
	}
	approxEq(t, "point", r.Increase.Point, 0)
	if len(r.Resets) != 0 {
		t.Fatalf("持平不应判重置，实际 %d 次", len(r.Resets))
	}
	// 空集合必须序列化为 [] 而不是 null。
	for _, field := range []string{"resets", "gaps"} {
		raw, _ := json.Marshal(r)
		var m map[string]any
		if err := json.Unmarshal(raw, &m); err != nil {
			t.Fatal(err)
		}
		if v, ok := m[field].([]any); !ok || len(v) != 0 {
			t.Fatalf("%s 应序列化为空数组, 实际 %v", field, m[field])
		}
	}
}

func TestComputeTooFewSamples(t *testing.T) {
	r, err := Compute([]series.Sample{{TimestampMs: baseMs, Value: 1}},
		baseMs, baseMs+100000, ExtrapNone, 0)
	if err != nil {
		t.Fatal(err)
	}
	if r.SampleCount != 1 || r.Increase.Point != 0 || r.Increase.Upper != nil {
		t.Fatalf("单样本报告异常: %+v", r)
	}
	r0, _ := Compute(nil, baseMs, baseMs+100000, ExtrapNone, 0)
	if r0.SampleCount != 0 {
		t.Fatalf("空报告异常: %+v", r0)
	}
}

func TestComputeInvalidWindow(t *testing.T) {
	if _, err := Compute(handSamples(), baseMs+100, baseMs, ExtrapNone, 0); err == nil {
		t.Fatal("非法窗口应报错")
	}
}

func TestParseExtrapolation(t *testing.T) {
	if e, err := ParseExtrapolation(""); err != nil || e != ExtrapClamped {
		t.Fatalf("默认策略错误: %v %v", e, err)
	}
	if _, err := ParseExtrapolation("bogus"); err == nil {
		t.Fatal("未知策略应报错")
	}
}

func TestBoundsMarshalInfAsNull(t *testing.T) {
	inf := math.Inf(1)
	b := Bounds{Point: 1, Lower: 1, Upper: &inf}
	raw, err := json.Marshal(b)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	if m["upper"] != nil {
		t.Fatalf("+Inf 应序列化为 null, 实际 %s", raw)
	}
}
