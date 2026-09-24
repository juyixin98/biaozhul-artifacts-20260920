// Package metricstub is a deterministic, scenario-driven stand-in for the real
// error/latency metrics pipeline. It serves raw per-request samples over HTTP
// so the decision engine performs genuine aggregation and interval math.
//
// Scenario time is anchored to the release creation timestamp the caller
// supplies (created_at), making the stub stateless and restart-safe: the
// response for (release, window) depends only on that window.
package metricstub

import (
	"encoding/json"
	"hash/fnv"
	"math"
	"math/rand"
	"net/http"
	"time"
)

const TickMS int64 = 200

const samplesPerBucket int64 = 55

// Scenarios accepted as ?scenario=.
var Scenarios = []string{"healthy", "spike", "spike-short", "degraded", "insufficient", "blackout", "late"}

type bucketSpec struct {
	samples int64
	errors  int64
	mean    float64
	sd      float64
}

// specAt returns the generator parameters for a bucket starting elapsed
// milliseconds after release creation.
func specAt(scenario string, elapsedMS int64) (bucketSpec, bool) {
	// false means "no bucket should be served at this wall time yet".
	healthy := bucketSpec{samplesPerBucket, 0, 50, 20}
	switch scenario {
	case "healthy", "late":
		return healthy, true
	case "spike":
		// A short, severe burst between t=1.0s and t=3.0s; before and after it
		// the service is healthy. Demonstrates that a transient spike fully
		// contained in a completed window shows as degraded, while one diluted
		// across a longer observation window passes.
		if elapsedMS >= 1000 && elapsedMS < 3000 {
			// A burst of ~5.5% errors and 170ms latency fully contained in a
			// short window shows degraded. The same burst diluted across a
			// longer window falls back under the frozen thresholds (healthy):
			// half-burst mean is ~110ms and error-rate Wilson upper ~3.9%.
			return bucketSpec{samplesPerBucket, 3, 170, 25}, true
		}
		return healthy, true
	case "spike-short":
		// A burst that begins immediately at stage entry and lasts 1.2s, so a
		// short first-stage observation window (e.g. 800ms) is fully inside it.
		if elapsedMS < 1200 {
			return bucketSpec{samplesPerBucket, 3, 170, 25}, true
		}
		return healthy, true
	case "degraded":
		// Persistent regression for the entire release: elevated error rate
		// and latency in every bucket. Never self-heals.
		return bucketSpec{samplesPerBucket, 25, 250, 40}, true
	case "insufficient":
		// Service runs but only a trickle of traffic arrives: 3 samples per
		// bucket vs a minimum of 80.
		return bucketSpec{3, 0, 50, 20}, true
	default:
		return healthy, true
	}
}

// sampleLatency generates one deterministic latency value keyed by
// (release, bucket index, sample index), clamped to a plausible range.
func sampleLatency(releaseID string, bucketK, sampleJ int64, mean, sd float64) float64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(releaseID))
	var buf [8]byte
	for _, v := range []int64{bucketK, sampleJ} {
		for i := 0; i < 8; i++ {
			buf[i] = byte(v >> (8 * i))
		}
		_, _ = h.Write(buf[:])
	}
	rng := rand.New(rand.NewSource(int64(h.Sum64() % (1 << 62))))
	v := mean + sd*rng.NormFloat64()
	return math.Max(1, math.Min(1000, v))
}

// Handler returns the http.Handler implementing GET /metrics and /healthz.
func Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		q := r.URL.Query()
		releaseID := q.Get("release_id")
		scenario := q.Get("scenario")
		from, err1 := time.Parse(time.RFC3339Nano, q.Get("from"))
		to, err2 := time.Parse(time.RFC3339Nano, q.Get("to"))
		t0, err3 := time.Parse(time.RFC3339Nano, q.Get("created_at"))
		if releaseID == "" || scenario == "" || err1 != nil || err2 != nil || err3 != nil || !to.After(from) {
			http.Error(w, `{"error":"bad request: need release_id, scenario, from, to, created_at"}`, http.StatusBadRequest)
			return
		}
		if scenario == "blackout" {
			// Explicit total outage: distinguish "unknown" from "healthy".
			w.WriteHeader(http.StatusServiceUnavailable)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"release_id":   releaseID,
				"scenario":     scenario,
				"tick_ms":      TickMS,
				"available":    false,
				"window_start": from,
				"window_end":   to,
				"buckets":      []any{},
				"reason":       "metrics pipeline blackout: no data can be produced for this release",
			})
			return
		}

		now := time.Now()
		tick := time.Duration(TickMS) * time.Millisecond

		type outBucket struct {
			T         time.Time `json:"t"`
			Samples   int64     `json:"samples"`
			Errors    int64     `json:"errors"`
			LatencyMS []float64 `json:"latency_ms"`
		}
		buckets := []outBucket{}
		// Buckets are indexed from release creation: bucket k covers
		// [t0+k*tick, t0+(k+1)*tick). We return buckets that have started
		// (start < now) and overlap the requested [from,to) range; for the
		// aligned windows the engine requests, that is exactly observation/
		// tick buckets tiling [from,to).
		// Candidate buckets from the full requested range; future buckets are
		// removed below by the start<now filter.
		firstK := int64(math.Ceil(float64(from.Sub(t0)) / float64(tick)))
		lastK := int64(math.Floor(float64(to.Sub(t0))/float64(tick))) - 1
		for k := firstK; k <= lastK; k++ {
			start := t0.Add(time.Duration(k) * tick)
			// Only buckets that have actually started (start < now) and whose
			// start lies in [from, to).
			if !start.Before(now) || start.Before(from) || !start.Before(to) {
				continue
			}
			elapsed := start.Sub(t0)
			spec, ok := specAt(scenario, elapsed.Milliseconds())
			if !ok {
				continue
			}
			lat := make([]float64, spec.samples)
			for j := int64(0); j < spec.samples; j++ {
				lat[j] = sampleLatency(releaseID, k, j, spec.mean, spec.sd)
			}
			buckets = append(buckets, outBucket{T: start, Samples: spec.samples, Errors: spec.errors, LatencyMS: lat})
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"release_id":   releaseID,
			"scenario":     scenario,
			"tick_ms":      TickMS,
			"window_start": from,
			"window_end":   to,
			"available":    true,
			"buckets":      buckets,
		})
	})
	return mux
}
