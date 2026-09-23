package governor

import (
	"fmt"
	"math/rand"
	"strings"
	"sync"
	"testing"
)

func testConfig() Config {
	c := DefaultConfig()
	c.DefaultSeriesBudget = 10
	c.MaxMetricNames = 8
	return c
}

// TestSeriesBudgetAndOverflow：预算内建组合；第 budget+1 个新组合进 overflow；
// overflow 样本不新建组合；已有组合永不被挤走。
func TestSeriesBudgetAndOverflow(t *testing.T) {
	s := NewStore(testConfig())

	// 建立 10 个不同组合。
	for i := 0; i < 10; i++ {
		r := s.Ingest(Sample{Metric: "m", Labels: map[string]string{"pod": fmt.Sprintf("pod-%d", i)}, Value: 1})
		if !r.Accepted || r.Overflowed {
			t.Fatalf("sample %d should be accepted as new tracked series: %+v", i, r)
		}
	}
	view, ok := s.QueryMetric("m")
	if !ok {
		t.Fatal("metric missing")
	}
	if view.TrackedSeries != 10 {
		t.Fatalf("tracked=%d want 10", view.TrackedSeries)
	}

	// 第 11..30 个全新组合必须全部 overflow，且 tracked 数量保持 10。
	for i := 10; i < 30; i++ {
		r := s.Ingest(Sample{Metric: "m", Labels: map[string]string{"pod": fmt.Sprintf("evil-%d", i)}, Value: 2})
		if !r.Accepted || !r.Overflowed {
			t.Fatalf("sample %d should overflow: %+v", i, r)
		}
	}
	view, _ = s.QueryMetric("m")
	if view.TrackedSeries != 10 {
		t.Fatalf("tracked=%d, existing series must never be evicted", view.TrackedSeries)
	}
	if view.Overflow == nil || view.Overflow.SampleCount != 20 {
		t.Fatalf("overflow bucket should hold 20 samples, got %+v", view.Overflow)
	}
	if view.Overflow.ValueSum != 40 {
		t.Fatalf("overflow value sum=%v want 40", view.Overflow.ValueSum)
	}
	if view.Overflow.Labels[OverflowLabel] != OverflowValue {
		t.Fatalf("overflow labels = %v", view.Overflow.Labels)
	}

	// 老组合仍然累加（没有被新标签挤走）。
	r := s.Ingest(Sample{Metric: "m", Labels: map[string]string{"pod": "pod-0"}, Value: 5})
	if r.Overflowed || !r.Accepted {
		t.Fatalf("old series must still be tracked: %+v", r)
	}
	view, _ = s.QueryMetric("m")
	var found *Series
	for i := range view.Series {
		if view.Series[i].Labels["pod"] == "pod-0" {
			found = &view.Series[i]
		}
	}
	if found == nil || found.SampleCount != 2 || found.ValueSum != 6 {
		t.Fatalf("old series not accumulated correctly: %+v", found)
	}
	if view.TrackedSeries != 10 {
		t.Fatalf("tracked=%d must stay at budget", view.TrackedSeries)
	}

	ctr := s.Snapshot()
	if ctr.SeriesEvicted != 0 {
		t.Fatalf("SeriesEvicted must always be 0, got %d", ctr.SeriesEvicted)
	}
	if err := ctr.CheckConservation(); err != nil {
		t.Fatal(err)
	}
	// received = 31；tracked 命中 = 10 建组合 + 1 老样本 = 11；overflow = 20。
	if ctr.Received != 31 || ctr.Accepted != 11 || ctr.Overflowed != 20 || ctr.Rejected != 0 {
		t.Fatalf("counters wrong: %+v", ctr)
	}
}

// TestPerMetricBudgetOverride：每个指标独立预算，覆盖配置惰性生效。
func TestPerMetricBudgetOverride(t *testing.T) {
	c := testConfig()
	c.MetricBudgets = map[string]int{"small": 2}
	s := NewStore(c)

	for i := 0; i < 5; i++ {
		s.Ingest(Sample{Metric: "small", Labels: map[string]string{"k": fmt.Sprintf("v%d", i)}, Value: 1})
		s.Ingest(Sample{Metric: "big", Labels: map[string]string{"k": fmt.Sprintf("v%d", i)}, Value: 1})
	}
	small, _ := s.QueryMetric("small")
	big, _ := s.QueryMetric("big")
	if small.TrackedSeries != 2 || small.Overflow == nil || small.Overflow.SampleCount != 3 {
		t.Fatalf("small metric budget not enforced: %+v", small)
	}
	if big.TrackedSeries != 5 || big.Overflow != nil {
		t.Fatalf("big metric should use default budget: %+v", big)
	}
}

// TestMetricNameBudget：全局指标名上限。
func TestMetricNameBudget(t *testing.T) {
	c := testConfig()
	c.MaxMetricNames = 2
	s := NewStore(c)

	for i := 0; i < 2; i++ {
		if r := s.Ingest(Sample{Metric: fmt.Sprintf("m%d", i), Value: 1}); !r.Accepted {
			t.Fatalf("metric %d should be accepted: %+v", i, r)
		}
	}
	r := s.Ingest(Sample{Metric: "m_evil", Labels: map[string]string{"uid": "x"}, Value: 1})
	if r.Accepted || r.Reason != ReasonMetricBudgetExceeded {
		t.Fatalf("third distinct metric should be rejected, got %+v", r)
	}
	// 已存在指标仍可写。
	if r := s.Ingest(Sample{Metric: "m0", Value: 1}); !r.Accepted {
		t.Fatalf("existing metric must remain writable: %+v", r)
	}
}

// TestLabelValidation：键名合法性、保留前缀、键数量、值截断/拒绝。
func TestLabelValidation(t *testing.T) {
	c := testConfig()
	c.MaxLabelsPerSeries = 2
	c.MaxLabelKeyBytes = 8
	c.MaxLabelValueBytes = 16
	c.TruncateLabelValues = true
	s := NewStore(c)

	cases := []struct {
		name   string
		sample Sample
		reason string
	}{
		{"bad metric", Sample{Metric: "1bad"}, ReasonInvalidMetricName},
		{"reserved key", Sample{Metric: "m", Labels: map[string]string{"__bucket__": "x"}}, ReasonReservedLabelKey},
		{"bad key", Sample{Metric: "m", Labels: map[string]string{"a.b": "x"}}, ReasonInvalidLabelKey},
		{"long key", Sample{Metric: "m", Labels: map[string]string{"abcdefghi": "x"}}, ReasonLabelKeyTooLong},
		{"too many", Sample{Metric: "m", Labels: map[string]string{"a": "1", "b": "2", "c": "3"}}, ReasonTooManyLabels},
	}
	for _, tc := range cases {
		if r := s.Ingest(tc.sample); r.Accepted || r.Reason != tc.reason {
			t.Fatalf("%s: want reason %s, got %+v", tc.name, tc.reason, r)
		}
	}

	// 超长值：默认截断到 16 字节且按 UTF-8 边界。
	long := "短い文字列テストabcdefghijklmnopqrstuvwxyz" // 前若干 rune 超 16 字节
	r := s.Ingest(Sample{Metric: "m", Labels: map[string]string{"k": long}, Value: 1})
	if !r.Accepted {
		t.Fatalf("truncated sample should be accepted: %+v", r)
	}
	v, _ := s.QueryMetric("m")
	got := v.Series[0].Labels["k"]
	if len(got) > 16 {
		t.Fatalf("truncated value is %d bytes: %q", len(got), got)
	}
	if !strings.HasPrefix(long, got) {
		t.Fatalf("truncation should be a prefix: %q vs %q", got, long)
	}
	if s.Snapshot().ValuesTruncated != 1 {
		t.Fatal("truncation counter not incremented")
	}

	// 关闭截断后：拒绝。
	c.TruncateLabelValues = false
	s2 := NewStore(c)
	if r := s2.Ingest(Sample{Metric: "m", Labels: map[string]string{"k": long}, Value: 1}); r.Accepted || r.Reason != ReasonLabelValueTooLong {
		t.Fatalf("want rejection with truncation off, got %+v", r)
	}
}

// TestTruncateUTF8 表驱动校验边界行为。
func TestTruncateUTF8(t *testing.T) {
	cases := []struct {
		in    string
		limit int
	}{
		{"héllo", 3}, // h + 两字节 é 的首字节应整体丢弃 => "h"
		{"abc", 10},
		{"短い", 3}, // 一个 3 字节 rune
		{"短い", 4}, // 只能放下第一个 rune
		{"短い", 6},
	}
	for _, tc := range cases {
		out := truncateUTF8(tc.in, tc.limit)
		if len(out) > tc.limit {
			t.Fatalf("len(%q)=%d > %d", out, len(out), tc.limit)
		}
		if !strings.HasPrefix(tc.in, out) {
			t.Fatalf("%q is not a prefix of %q", out, tc.in)
		}
	}
}

// TestIngestCopiesLabels：调用方在摄入后修改 map 不得影响存储。
func TestIngestCopiesLabels(t *testing.T) {
	s := NewStore(testConfig())
	labels := map[string]string{"pod": "p0"}
	s.Ingest(Sample{Metric: "m", Labels: labels, Value: 1})
	labels["pod"] = "mutated"
	labels["injected"] = "x"
	view, _ := s.QueryMetric("m")
	if got := view.Series[0].Labels["pod"]; got != "p0" {
		t.Fatalf("store labels mutated by caller: %q", got)
	}
	if _, ok := view.Series[0].Labels["injected"]; ok {
		t.Fatal("caller injected a label after ingest")
	}
}

// TestConcurrentIngest：并发摄入下计数守恒、组合数不越界。
func TestConcurrentIngest(t *testing.T) {
	c := testConfig()
	c.DefaultSeriesBudget = 64
	c.MaxMetricNames = 16
	s := NewStore(c)

	const goroutines = 16
	const perG = 2000
	var wg sync.WaitGroup
	for g := 0; g < goroutines; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			rng := rand.New(rand.NewSource(int64(g)))
			for i := 0; i < perG; i++ {
				s.Ingest(Sample{
					Metric: fmt.Sprintf("m%d", g%4),
					Labels: map[string]string{
						"uid": fmt.Sprintf("%d", rng.Intn(500)), // 高基数尝试
						"g":   fmt.Sprintf("%d", g),
					},
					Value: 1,
				})
			}
		}(g)
	}
	wg.Wait()

	ctr := s.Snapshot()
	if err := ctr.CheckConservation(); err != nil {
		t.Fatal(err)
	}
	if ctr.Received != int64(goroutines*perG) {
		t.Fatalf("received=%d want %d", ctr.Received, goroutines*perG)
	}
	for _, name := range s.MetricNames() {
		view, _ := s.QueryMetric(name)
		if view.TrackedSeries > 64 {
			t.Fatalf("metric %s tracked %d > budget 64", name, view.TrackedSeries)
		}
	}
	// 每个指标 (g%4)：g 标签 4 种 × uid 500 种 => 远超 64，必有 overflow。
	if ctr.Overflowed == 0 {
		t.Fatal("expected overflow under concurrent high-cardinality load")
	}
}
