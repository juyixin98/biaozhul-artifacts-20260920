package store

import (
	"fmt"
	"math"
	"testing"
)

func testConfig() Config {
	return Config{
		MaxSeriesPerMetric: 3,
		MaxMetricNames:     2,
		MaxMetricNameLen:   32,
		MaxLabelKeys:       4,
		MaxLabelKeyLen:     16,
		MaxLabelValueLen:   8,
	}
}

func ingestN(t *testing.T, st *Store, samples ...Sample) []Outcome {
	t.Helper()
	outs := st.IngestBatch(samples)
	if len(outs) != len(samples) {
		t.Fatalf("outcomes %d != samples %d", len(outs), len(samples))
	}
	return outs
}

func TestAdmitUpToBudgetThenOverflow(t *testing.T) {
	st, _ := New(testConfig())

	// Budget is 3: first three distinct combos admitted as normal series.
	for i := 0; i < 3; i++ {
		o := st.Ingest(Sample{Metric: "m", Labels: map[string]string{"k": fmt.Sprintf("v%d", i)}, Value: 1})
		if !o.Accepted || o.Overflow || !o.NewSeries {
			t.Fatalf("sample %d: %+v", i, o)
		}
	}
	// 4th distinct combo -> overflow.
	o := st.Ingest(Sample{Metric: "m", Labels: map[string]string{"k": "vX"}, Value: 2})
	if !o.Accepted || !o.Overflow {
		t.Fatalf("expected overflow, got %+v", o)
	}
	// 5th distinct combo -> same overflow bucket.
	o = st.Ingest(Sample{Metric: "m", Labels: map[string]string{"k": "vY"}, Value: 3})
	if !o.Accepted || !o.Overflow {
		t.Fatalf("expected overflow reuse, got %+v", o)
	}

	mv, ok := st.Metric("m", true)
	if !ok {
		t.Fatal("metric missing")
	}
	if !mv.AtBudget || mv.Budget != 3 {
		t.Fatalf("metric should report at_budget: %+v", mv)
	}
	if mv.NormalSeries != 3 {
		t.Fatalf("normal series = %d, want 3", mv.NormalSeries)
	}
	if mv.Overflow == nil || mv.Overflow.Count != 2 {
		t.Fatalf("overflow = %+v, want count 2", mv.Overflow)
	}
	if len(mv.Series) != 4 {
		t.Fatalf("series entries = %d, want 4 (3 normal + overflow)", len(mv.Series))
	}
	// Overflow sum preserved.
	if mv.Overflow.Sum != 5 {
		t.Fatalf("overflow sum = %v, want 5", mv.Overflow.Sum)
	}
}

func TestExistingCombinationsNotEvicted(t *testing.T) {
	st, _ := New(testConfig())

	// Admit combos v0,v1,v2, then flood with unique combos to overflow.
	for i := 0; i < 3; i++ {
		st.Ingest(Sample{Metric: "m", Labels: map[string]string{"k": fmt.Sprintf("v%d", i)}, Value: 1})
	}
	for i := 0; i < 100; i++ {
		st.Ingest(Sample{Metric: "m", Labels: map[string]string{"k": fmt.Sprintf("flood%d", i)}, Value: 1})
	}

	// Re-send each original combo: counts must increment in place, and no
	// new series may be created.
	for i := 0; i < 3; i++ {
		o := st.Ingest(Sample{Metric: "m", Labels: map[string]string{"k": fmt.Sprintf("v%d", i)}, Value: 10})
		if !o.Accepted || o.Overflow || o.NewSeries {
			t.Fatalf("combo v%d displaced: %+v", i, o)
		}
	}
	mv, _ := st.Metric("m", true)
	if mv.NormalSeries != 3 {
		t.Fatalf("normal series = %d, want 3 (no eviction)", mv.NormalSeries)
	}
	if mv.Overflow == nil || mv.Overflow.Count != 100 {
		t.Fatalf("overflow count = %v, want 100", mv.Overflow)
	}
	var normalCount int64
	for _, sr := range mv.Series {
		if !sr.Overflow {
			if sr.Count != 2 {
				t.Fatalf("series %q count %d, want 2", sr.Key, sr.Count)
			}
			if sr.Sum != 11 {
				t.Fatalf("series %q sum %v, want 11", sr.Key, sr.Sum)
			}
			normalCount += sr.Count
		}
	}
	if normalCount != 6 {
		t.Fatalf("normal count = %d, want 6", normalCount)
	}
}

func TestCountConservation(t *testing.T) {
	st, _ := New(testConfig())

	const normal, attack = 20, 200
	for i := 0; i < normal; i++ {
		st.Ingest(Sample{Metric: "m", Labels: map[string]string{"k": fmt.Sprintf("keep%d", i%3)}, Value: 1})
	}
	for i := 0; i < attack; i++ {
		st.Ingest(Sample{Metric: "m", Labels: map[string]string{"k": fmt.Sprintf("atk%d", i)}, Value: 1})
	}
	// Some invalid samples too.
	st.Ingest(Sample{Metric: ""})
	st.Ingest(Sample{Metric: "m", Labels: map[string]string{"": "v"}})

	stStats := st.Stats()
	if stStats.SamplesReceived != normal+attack+2 {
		t.Fatalf("received %d", stStats.SamplesReceived)
	}
	if stStats.SamplesAccepted+stStats.SamplesRejected != stStats.SamplesReceived {
		t.Fatalf("accepted+rejected != received: %+v", stStats)
	}
	if stStats.OverflowSamples+stStats.NormalSamples != stStats.SamplesAccepted {
		t.Fatalf("overflow+normal != accepted: %+v", stStats)
	}
	mv, _ := st.Metric("m", true)
	var seriesCount int64
	for _, sr := range mv.Series {
		seriesCount += sr.Count
	}
	if seriesCount != mv.Count {
		t.Fatalf("per-series total %d != metric count %d", seriesCount, mv.Count)
	}
	if mv.Count != normal+attack {
		t.Fatalf("metric count %d, want %d", mv.Count, normal+attack)
	}
	// The 20 stable samples cycle among the 3 admitted combos; all 200
	// unique attack combos overflow.
	if mv.Overflow.Count != 200 {
		t.Fatalf("overflow %d, want 200", mv.Overflow.Count)
	}
}

func TestMetricNameBudget(t *testing.T) {
	cfg := testConfig()
	st, _ := New(cfg)

	o1 := st.Ingest(Sample{Metric: "a"})
	o2 := st.Ingest(Sample{Metric: "b"})
	o3 := st.Ingest(Sample{Metric: "c"})
	if !o1.Accepted || !o2.Accepted {
		t.Fatalf("a/b should be admitted: %+v %+v", o1, o2)
	}
	if o3.Accepted || o3.Reason != ReasonMetricBudget {
		t.Fatalf("c should hit metric budget, got %+v", o3)
	}
	// Existing metric still writable.
	if o := st.Ingest(Sample{Metric: "a", Labels: map[string]string{"k": "v"}}); !o.Accepted {
		t.Fatalf("existing metric rejected: %+v", o)
	}
}

func TestValidationRejections(t *testing.T) {
	st, _ := New(testConfig())
	long33 := "0123456789012345678901234567890123"
	cases := []struct {
		name   string
		sample Sample
		reason string
	}{
		{"empty metric", Sample{Metric: ""}, ReasonEmptyMetric},
		{"long metric", Sample{Metric: long33}, ReasonMetricTooLong},
		{"too many labels", Sample{Metric: "m", Labels: map[string]string{"a": "1", "b": "2", "c": "3", "d": "4", "e": "5"}}, ReasonTooManyLabels},
		{"empty label key", Sample{Metric: "m", Labels: map[string]string{"": "v"}}, ReasonEmptyLabelKey},
		{"long label key", Sample{Metric: "m", Labels: map[string]string{"012345678901234567": "v"}}, ReasonLabelKeyTooLong},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			o := st.Ingest(tc.sample)
			if o.Accepted || o.Reason != tc.reason {
				t.Fatalf("got %+v, want reason %s", o, tc.reason)
			}
		})
	}
	if got := st.Stats().SamplesRejected; got != int64(len(cases)) {
		t.Fatalf("rejected = %d, want %d", got, len(cases))
	}
}

func TestLabelValueTruncation(t *testing.T) {
	st, _ := New(testConfig())

	// Value longer than 8 bytes: truncated, sample still admitted.
	o := st.Ingest(Sample{Metric: "m", Labels: map[string]string{"k": "abcdefghijklmnop"}, Value: 1})
	if !o.Accepted || !o.Truncated {
		t.Fatalf("expected truncation flag: %+v", o)
	}
	// The same long value maps to the same truncated series (no cardinality
	// leak through suffixes).
	o2 := st.Ingest(Sample{Metric: "m", Labels: map[string]string{"k": "abcdefghZZZzzz!!"}, Value: 1})
	if o2.NewSeries || o2.Overflow {
		t.Fatalf("truncated values must share a series: %+v", o2)
	}
	mv, _ := st.Metric("m", true)
	for _, sr := range mv.Series {
		if v := sr.Labels["k"]; len(v) > 8 {
			t.Fatalf("stored value %q exceeds limit", v)
		}
	}
	if st.Stats().TruncatedLabelValues != 2 {
		t.Fatalf("truncation counter = %d, want 2", st.Stats().TruncatedLabelValues)
	}
}

func TestTruncationKeepsUTF8Boundary(t *testing.T) {
	cfg := testConfig()
	cfg.MaxLabelValueLen = 4
	st, _ := New(cfg)
	// 2-byte runes at the cut point.
	st.Ingest(Sample{Metric: "m", Labels: map[string]string{"k": "abéééé"}, Value: 1})
	mv, _ := st.Metric("m", true)
	v := mv.Series[0].Labels["k"]
	if len(v) > 4 {
		t.Fatalf("truncated to %d bytes", len(v))
	}
	if !validUTF8(v) {
		t.Fatalf("truncation split a rune: %q", v)
	}
}

func validUTF8(s string) bool {
	for _, r := range s {
		if r == '�' {
			return false
		}
	}
	return true
}

func TestSeriesKeyCollisionFree(t *testing.T) {
	// Length prefixes must make these distinct.
	k1 := seriesKey(map[string]string{"ab": "c"})
	k2 := seriesKey(map[string]string{"a": "bc"})
	if k1 == k2 {
		t.Fatalf("key collision: %q", k1)
	}
	// Order independence.
	a := seriesKey(map[string]string{"x": "1", "y": "2"})
	b := seriesKey(map[string]string{"y": "2", "x": "1"})
	if a != b {
		t.Fatalf("keys differ by map order: %q vs %q", a, b)
	}
}

func TestEmptyLabelsIsStableSeries(t *testing.T) {
	st, _ := New(testConfig())
	o1 := st.Ingest(Sample{Metric: "m", Value: 1})
	o2 := st.Ingest(Sample{Metric: "m", Value: 2})
	if !o1.NewSeries || o2.NewSeries {
		t.Fatalf("empty-label samples should share one series: %+v %+v", o1, o2)
	}
	mv, _ := st.Metric("m", true)
	if mv.NormalSeries != 1 || mv.Count != 2 {
		t.Fatalf("view = %+v", mv)
	}
	if math.Abs(mv.Series[0].Sum-3) > 1e-9 {
		t.Fatalf("sum %v", mv.Series[0].Sum)
	}
}

func TestBudgetsIndependentPerMetric(t *testing.T) {
	st, _ := New(testConfig())
	for i := 0; i < 3; i++ {
		for _, name := range []string{"a", "b"} {
			o := st.Ingest(Sample{Metric: name, Labels: map[string]string{"k": fmt.Sprintf("v%d", i)}})
			if !o.Accepted || o.Overflow {
				t.Fatalf("%s sample %d: %+v", name, i, o)
			}
		}
	}
	for _, name := range []string{"a", "b"} {
		o := st.Ingest(Sample{Metric: name, Labels: map[string]string{"k": "overflow-now"}})
		if !o.Overflow {
			t.Fatalf("%s expected own overflow: %+v", name, o)
		}
	}
	stStats := st.Stats()
	if stStats.OverflowSeriesTotal != 2 {
		t.Fatalf("overflow buckets = %d, want 2", stStats.OverflowSeriesTotal)
	}
}
