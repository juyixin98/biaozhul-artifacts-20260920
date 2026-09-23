package governor_test

import (
	"fmt"
	"runtime"
	"testing"

	"cardinalgov/internal/governor"
)

// TestHighCardinalityAttackMemoryBounded 是验收核心夹具：
// 在极小预算（64 个组合）下灌入 50 万个唯一标签组合的攻击样本（分两波），
// 断言：
//  1. tracked 组合数恒等于预算（不含 overflow 桶），内存不随攻击线性增长；
//  2. 攻击前后（含两波之间）强制 GC，HeapAlloc 增量有界；
//  3. 攻击前建立的稳定组合全程保留、计数正确（未被新标签挤走）；
//  4. 计数守恒 Received == TrackedAccepted + Overflowed + Rejected。
func TestHighCardinalityAttackMemoryBounded(t *testing.T) {
	const budget = 64
	const attackPerWave = 250000

	cfg := governor.DefaultConfig()
	cfg.DefaultSeriesBudget = budget
	cfg.MaxMetricNames = 4
	s := governor.NewStore(cfg)

	// 基座：budget 个稳定组合，各写 3 次。
	for i := 0; i < budget; i++ {
		labels := map[string]string{"route": fmt.Sprintf("/r/%d", i%8), "pod": fmt.Sprintf("pod-%03d", i)}
		for range 3 {
			if r := s.Ingest(governor.Sample{Metric: "http_requests", Labels: labels, Value: 1}); !r.Accepted || r.Overflowed {
				t.Fatalf("stable setup rejected: %+v", r)
			}
		}
	}

	runtime.GC()
	var m0 runtime.MemStats
	runtime.ReadMemStats(&m0)

	fire := func(wave int) {
		for i := 0; i < attackPerWave; i++ {
			r := s.Ingest(governor.Sample{
				Metric: "http_requests",
				Labels: map[string]string{
					"route":      "/login",
					"request_id": fmt.Sprintf("wave%d-req-%09d", wave, i), // 每样本唯一
				},
				Value: 1,
			})
			if !r.Accepted || !r.Overflowed {
				t.Fatalf("attack sample %d (wave %d) not overflowed: %+v", i, wave, r)
			}
		}
	}
	fire(1)

	view, ok := s.QueryMetric("http_requests")
	if !ok {
		t.Fatal("metric missing")
	}
	if view.TrackedSeries != budget {
		t.Fatalf("tracked=%d, must stay at budget %d during attack", view.TrackedSeries, budget)
	}

	runtime.GC()
	var m1 runtime.MemStats
	runtime.ReadMemStats(&m1)

	fire(2)

	view, _ = s.QueryMetric("http_requests")
	if view.TrackedSeries != budget {
		t.Fatalf("tracked=%d after wave 2, want %d", view.TrackedSeries, budget)
	}
	if view.Overflow == nil || view.Overflow.SampleCount != 2*attackPerWave {
		t.Fatalf("overflow should hold %d samples, got %+v", 2*attackPerWave, view.Overflow)
	}

	// 老组合必须仍在且每个 3 次（第 3 次在波次后再补，见下）。
	stableHits := map[string]int64{}
	for _, ser := range view.Series {
		stableHits[ser.Labels["pod"]] = ser.SampleCount
	}
	for i := 0; i < budget; i++ {
		pod := fmt.Sprintf("pod-%03d", i)
		if stableHits[pod] != 3 {
			t.Fatalf("stable series %s evicted/corrupted: count=%d", pod, stableHits[pod])
		}
	}

	runtime.GC()
	var m2 runtime.MemStats
	runtime.ReadMemStats(&m2)

	grow1 := int64(m1.HeapAlloc) - int64(m0.HeapAlloc)
	grow2 := int64(m2.HeapAlloc) - int64(m1.HeapAlloc)
	total := int64(m2.HeapAlloc) - int64(m0.HeapAlloc)
	t.Logf("HeapAlloc before=%dKiB afterWave1=%dKiB afterWave2=%dKiB grow1=%dKiB grow2=%dKiB total=%dKiB (attacked %d unique series)",
		m0.HeapAlloc/1024, m1.HeapAlloc/1024, m2.HeapAlloc/1024, grow1/1024, grow2/1024, total/1024, 2*attackPerWave)

	// 有界判据：
	//   - 第二波 25 万个全新唯一组合不得带来显著堆增长（上限 8 MiB，
	//     给 GC/噪声留余量；若每个攻击样本都建组合，增量将达数百 MiB）；
	//   - 两波合计增量同样有界（16 MiB）。
	const perWaveBound = 8 << 20
	const totalBound = 16 << 20
	if grow2 > perWaveBound {
		t.Fatalf("heap grew %d KiB during wave 2 (> %d KiB): memory is unbounded", grow2/1024, perWaveBound/1024)
	}
	if total > totalBound {
		t.Fatalf("total heap growth %d KiB > %d KiB: memory is unbounded", total/1024, totalBound/1024)
	}

	// 攻击后再写老组合，仍走 tracked 路径。
	r := s.Ingest(governor.Sample{Metric: "http_requests", Labels: map[string]string{"route": "/r/0", "pod": "pod-000"}, Value: 1})
	if !r.Accepted || r.Overflowed {
		t.Fatalf("old series should still be tracked after attack: %+v", r)
	}

	ctr := s.Snapshot()
	if err := ctr.CheckConservation(); err != nil {
		t.Fatal(err)
	}
	// 3*budget tracked 命中 + 2*attack overflow + 1 tracked 命中。
	if ctr.Received != int64(3*budget+2*attackPerWave+1) {
		t.Fatalf("received=%d", ctr.Received)
	}
	if ctr.Accepted != int64(3*budget+1) || ctr.Overflowed != int64(2*attackPerWave) {
		t.Fatalf("accepted=%d overflowed=%d", ctr.Accepted, ctr.Overflowed)
	}
	if ctr.SeriesCreated != budget {
		t.Fatalf("series_created=%d want exactly %d", ctr.SeriesCreated, budget)
	}
	if ctr.SeriesEvicted != 0 {
		t.Fatal("no series may ever be evicted")
	}
}
