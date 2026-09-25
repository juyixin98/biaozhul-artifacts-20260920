package store

import (
	"fmt"
	"runtime"
	"sync"
	"testing"
)

// This is the high-cardinality attack acceptance fixture.
//
// An attacker sends N waves where EVERY sample carries a globally unique
// request_id. Without cardinality governance the store's retained memory
// grows linearly with the number of samples; with the per-metric budget the
// retained footprint must plateau after the budget fills and every later
// unique combination lands in the overflow bucket, while counters stay
// conserved.
func TestHighCardinalityAttackMemoryBounded(t *testing.T) {
	cfg := Config{
		MaxSeriesPerMetric: 100,
		MaxMetricNames:     2,
		MaxMetricNameLen:   64,
		MaxLabelKeys:       8,
		MaxLabelKeyLen:     32,
		MaxLabelValueLen:   64,
	}
	st, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}

	// stableLabels returns one of 50 genuinely distinct stable combos.
	stableLabels := func(i int) map[string]string {
		return map[string]string{
			"method": []string{"GET", "POST"}[i%2],
			"path":   fmt.Sprintf("/p/%d", i%10),
			"host":   fmt.Sprintf("host-%d", i%5),
			"shard":  fmt.Sprintf("s%d", i), // makes each of the 50 unique
		}
	}
	attackLabels := func(unique int) map[string]string {
		return map[string]string{
			"method":     "GET",
			"path":       "/login",
			"request_id": fmt.Sprintf("attacker-unique-%d", unique),
		}
	}
	fire := func(stable, attackUnique int, idBase int) {
		samples := make([]Sample, 0, stable+attackUnique)
		for i := 0; i < stable; i++ {
			samples = append(samples, Sample{Metric: "http_requests", Labels: stableLabels(i), Value: 1})
		}
		for i := 0; i < attackUnique; i++ {
			samples = append(samples, Sample{Metric: "http_requests", Labels: attackLabels(idBase + i), Value: 1})
		}
		st.IngestBatch(samples)
	}

	// Warm up: fill the 100-series budget exactly (50 stable + 50 attack),
	// then one more attack sample to create the overflow bucket. Retained
	// state is now at its plateau.
	fire(50, 50, 0)
	fire(0, 1, 50)
	baseline := st.Stats()
	if baseline.SeriesTotal != 101 {
		t.Fatalf("setup: series = %d, want 101 (100 normal + overflow)", baseline.SeriesTotal)
	}

	runtime.GC()
	var heapBefore runtime.MemStats
	runtime.ReadMemStats(&heapBefore)
	logicalBefore := baseline.LogicalBytes

	// THE ATTACK: 10 waves, 5,000 brand-new unique combos each = 50k hostile
	// samples, all with unique ~64-byte request IDs.
	const waves, perWave = 10, 5000
	var totalSent int64 = 101
	nextID := 51
	for w := 0; w < waves; w++ {
		fire(50, perWave, nextID)
		nextID += perWave
		totalSent += 50 + perWave
	}

	after := st.Stats()
	runtime.GC()
	var heapAfter runtime.MemStats
	runtime.ReadMemStats(&heapAfter)

	// --- Acceptance criterion 1: retained logical footprint is flat. ---
	if after.SeriesTotal != 101 { // 100 normal + 1 overflow bucket
		t.Fatalf("after attack: series total = %d, want 101 (budget + overflow)", after.SeriesTotal)
	}
	if after.LogicalBytes != logicalBefore {
		t.Fatalf("logical footprint grew: before=%d after=%d (must be constant)",
			logicalBefore, after.LogicalBytes)
	}
	if after.LogicalBytes > after.LogicalBytesBound {
		t.Fatalf("footprint %d exceeds configured hard bound %d",
			after.LogicalBytes, after.LogicalBytesBound)
	}

	// Heap must not grow proportionally to 50k attack samples. The retained
	// state is bounded by config; give generous slack for allocator noise.
	heapGrowth := int64(heapAfter.HeapAlloc) - int64(heapBefore.HeapAlloc)
	attackPayloadBytes := int64(waves * perWave * 64) // unique request IDs alone
	t.Logf("heap before=%d after=%d growth=%d (unbounded growth would be ~%d); logical=%d bound=%d",
		heapBefore.HeapAlloc, heapAfter.HeapAlloc, heapGrowth, attackPayloadBytes,
		after.LogicalBytes, after.LogicalBytesBound)
	if heapGrowth > 32<<20 {
		t.Fatalf("heap grew %d bytes during attack; retained state must be bounded", heapGrowth)
	}
	if heapGrowth > attackPayloadBytes/4 {
		t.Fatalf("heap grew %d, more than 25%% of the %d-byte attack payload; not bounded",
			heapGrowth, attackPayloadBytes)
	}

	// --- Acceptance criterion 2: count conservation across overflow. ---
	mv, ok := st.Metric("http_requests", true)
	if !ok {
		t.Fatal("metric missing")
	}
	var seriesCount int64
	for _, sr := range mv.Series {
		seriesCount += sr.Count
	}
	if seriesCount != mv.Count {
		t.Fatalf("per-series total %d != metric count %d", seriesCount, mv.Count)
	}
	if mv.Count != totalSent {
		t.Fatalf("metric count %d != samples sent %d", mv.Count, totalSent)
	}
	if after.SamplesAccepted != totalSent {
		t.Fatalf("accepted %d != sent %d", after.SamplesAccepted, totalSent)
	}
	if after.SamplesAccepted+after.SamplesRejected != after.SamplesReceived {
		t.Fatalf("global counters not conserving: %+v", after)
	}
	expectedOverflow := int64(1 + waves*perWave)
	if mv.Overflow == nil || mv.Overflow.Count != expectedOverflow {
		t.Fatalf("overflow count = %v, want %d", mv.Overflow, expectedOverflow)
	}
	if after.NormalSamples+after.OverflowSamples != after.SamplesAccepted {
		t.Fatalf("normal+overflow != accepted: %+v", after)
	}

	// --- Acceptance criterion 3: stable combos still readable, not evicted. ---
	for i := 0; i < 50; i++ {
		key := seriesKey(stableLabels(i))
		var found bool
		for _, sr := range mv.Series {
			if sr.Key == key {
				if sr.Count != int64(waves+1) {
					t.Fatalf("stable series %q count %d, want %d", key, sr.Count, waves+1)
				}
				found = true
			}
		}
		if !found {
			t.Fatalf("stable combination %q evicted by attack labels", key)
		}
	}

	// One more wave: stats are identical, plateau confirmed.
	fire(50, perWave, nextID)
	plateau := st.Stats()
	if plateau.SeriesTotal != after.SeriesTotal || plateau.LogicalBytes != after.LogicalBytes {
		t.Fatalf("footprint not at plateau: %+v vs %+v", plateau, after)
	}
}

// TestConcurrentIngest races parallel writers and asserts conservation under
// the race detector (go test -race).
func TestConcurrentIngest(t *testing.T) {
	cfg := Config{
		MaxSeriesPerMetric: 50,
		MaxMetricNames:     4,
		MaxMetricNameLen:   32,
		MaxLabelKeys:       4,
		MaxLabelKeyLen:     16,
		MaxLabelValueLen:   16,
	}
	st, _ := New(cfg)

	const goroutines, perG = 16, 2000
	var wg sync.WaitGroup
	for g := 0; g < goroutines; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			samples := make([]Sample, perG)
			for i := 0; i < perG; i++ {
				samples[i] = Sample{
					Metric: fmt.Sprintf("metric-%d", i%3),
					Labels: map[string]string{
						"stable": fmt.Sprintf("s%d", i%10),
						"uniq":   fmt.Sprintf("g%d-i%d", g, i), // high cardinality
					},
					Value: float64(i),
				}
			}
			st.IngestBatch(samples)
		}(g)
	}
	wg.Wait()

	stStats := st.Stats()
	want := int64(goroutines * perG)
	if stStats.SamplesReceived != want {
		t.Fatalf("received %d, want %d", stStats.SamplesReceived, want)
	}
	if stStats.SamplesAccepted != want {
		t.Fatalf("accepted %d, want %d (all samples valid)", stStats.SamplesAccepted, want)
	}
	if stStats.NormalSamples+stStats.OverflowSamples != want {
		t.Fatalf("normal+overflow != total: %+v", stStats)
	}

	// Per-metric conservation.
	var perMetric int64
	for _, mv := range st.Metrics() {
		if mv.NormalSeries > cfg.MaxSeriesPerMetric {
			t.Fatalf("%s: %d series over budget %d", mv.Name, mv.NormalSeries, cfg.MaxSeriesPerMetric)
		}
		perMetric += mv.Count
	}
	if perMetric != want {
		t.Fatalf("per-metric totals %d != accepted %d", perMetric, want)
	}

	// Re-read all metrics with series detail to exercise concurrent-safe
	// view copies after the writes.
	for _, mv := range st.Metrics() {
		detailed, ok := st.Metric(mv.Name, true)
		if !ok {
			t.Fatalf("metric %s vanished", mv.Name)
		}
		var sum int64
		for _, sr := range detailed.Series {
			sum += sr.Count
		}
		if sum != detailed.Count {
			t.Fatalf("%s: series total %d != count %d", mv.Name, sum, detailed.Count)
		}
	}
}
