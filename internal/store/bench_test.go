package store

import (
	"fmt"
	"runtime"
	"testing"
)

// BenchmarkAttackIngest measures ingest throughput while a hostile
// high-cardinality workload is running against a saturated budget. After the
// first `budget` unique combos, every subsequent sample is overflow work
// (constant retained state).
func BenchmarkAttackIngest(b *testing.B) {
	cfg := Config{
		MaxSeriesPerMetric: 1000,
		MaxMetricNames:     1,
		MaxMetricNameLen:   32,
		MaxLabelKeys:       8,
		MaxLabelKeyLen:     32,
		MaxLabelValueLen:   64,
	}
	st, _ := New(cfg)

	// Pre-fill the budget so the benchmark measures steady-state overflow.
	for i := 0; i < cfg.MaxSeriesPerMetric; i++ {
		st.Ingest(Sample{Metric: "m", Labels: map[string]string{"id": fmt.Sprintf("fill-%d", i)}})
	}

	samples := make([]Sample, 400)
	b.ReportAllocs()
	b.ResetTimer()
	var n int
	for i := 0; i < b.N; i++ {
		for j := range samples {
			samples[j] = Sample{Metric: "m", Labels: map[string]string{"id": fmt.Sprintf("attack-%d-%d", i, j)}}
		}
		st.IngestBatch(samples)
		n += len(samples)
	}
	b.StopTimer()

	runtime.GC()
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	b.ReportMetric(float64(ms.HeapAlloc), "retained-heap-bytes")
	b.ReportMetric(float64(n), "samples")
}
