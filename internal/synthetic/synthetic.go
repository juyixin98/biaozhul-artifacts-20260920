// Package synthetic generates deterministic cumulative histogram samples for
// demos and tests. There are two deliberately *different* bucket layouts
// ("fine" and "coarse") so that boundary contraction is exercised.
package synthetic

import (
	"math"
	"math/rand"
	"time"

	"histmerge/internal/histogram"
	"histmerge/internal/store"
)

// FineBounds is a fine-grained latency layout (seconds).
var FineBounds = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10}

// CoarseBounds shares only 0.005? no — it shares the bold subset with Fine:
// shared finite bounds are 0.01, 0.1, 0.5, 2.5, 10. It also adds 30.
var CoarseBounds = []float64{0.01, 0.03, 0.1, 0.3, 0.5, 1.5, 2.5, 10, 30}

func buildCumulative(upperFinite []float64, observations []float64) *histogram.Histogram {
	type bc struct {
		u histogram.Bound
		c uint64
	}
	bcs := make([]bc, 0, len(upperFinite)+1)
	for _, u := range upperFinite {
		bcs = append(bcs, bc{u: histogram.FiniteBound(u)})
	}
	bcs = append(bcs, bc{u: histogram.InfBound()})
	var sum float64
	for _, v := range observations {
		sum += v
		for i := range bcs {
			if bcs[i].u.IsInf() || v <= bcs[i].u.Value {
				for j := i; j < len(bcs); j++ {
					bcs[j].c++
				}
				break
			}
		}
	}
	h := &histogram.Histogram{TotalCount: uint64(len(observations)), Sum: sum}
	for _, b := range bcs {
		h.Buckets = append(h.Buckets, histogram.Bucket{Upper: b.u, CumulativeCount: b.c})
	}
	return h
}

// Generate produces `scrapes` timestamped samples per series. Counts are
// cumulative across scrapes (each scrape observes new latency draws appended
// to the running history), so ingest monotonicity holds. seed makes it
// reproducible.
func Generate(base time.Time, scrapes int, seed int64) ([]store.Sample, error) {
	rng := rand.New(rand.NewSource(seed))
	var out []store.Sample

	mk := func(service, route string, bounds []float64, mean float64) {
		var history []float64
		for i := 0; i < scrapes; i++ {
			// 30-80 new observations per scrape; a couple land above the top
			// finite bound so the +Inf bucket is always exercised.
			n := 30 + rng.Intn(50)
			for k := 0; k < n; k++ {
				v := math.Abs(rng.NormFloat64()*mean + mean)
				if k == 0 {
					v += bounds[len(bounds)-1] * 2 // guarantee a +Inf hit
				}
				history = append(history, v)
			}
			// Work on a prefix snapshot so each scrape's histogram is the
			// cumulative state at that time.
			snap := append([]float64(nil), history...)
			h := buildCumulative(bounds, snap)
			if err := h.Validate(); err != nil {
				panic("synthetic produced invalid histogram: " + err.Error())
			}
			out = append(out, store.Sample{
				Timestamp: base.Add(time.Duration(i) * time.Minute),
				Labels: []store.Label{
					{Name: "service", Value: service},
					{Name: "route", Value: route},
				},
				Histogram: h,
			})
		}
	}

	mk("checkout", "/api/pay", FineBounds, 0.08)
	mk("checkout", "/api/cart", CoarseBounds, 0.2)
	mk("billing", "/api/invoice", FineBounds, 0.15)
	return out, nil
}

// EmptySample returns a valid empty cumulative histogram on the fine layout
// for a service that never observed traffic.
func EmptySample(t time.Time, service string) store.Sample {
	h := &histogram.Histogram{}
	for _, u := range FineBounds {
		h.Buckets = append(h.Buckets, histogram.Bucket{Upper: histogram.FiniteBound(u)})
	}
	h.Buckets = append(h.Buckets, histogram.Bucket{Upper: histogram.InfBound()})
	return store.Sample{
		Timestamp: t,
		Labels:    []store.Label{{Name: "service", Value: service}, {Name: "route", Value: "/health"}},
		Histogram: h,
	}
}
