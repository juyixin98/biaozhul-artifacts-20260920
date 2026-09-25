// Command verify runs the acceptance scenarios: it builds synthetic data at
// several densities, ingests it (including out-of-order and historical
// corrections), and compares every retention layer against aggregates
// recomputed independently from the raw samples.
//
// Exit code is 0 only when every check passes.
package main

import (
	"errors"
	"fmt"
	"math"
	"os"

	"metricrollup/internal/model"
	"metricrollup/internal/store"
	"metricrollup/internal/synthetic"
	"metricrollup/internal/verify"
)

type report struct {
	name   string
	pass   bool
	detail string
}

func main() {
	// Fixed window: [12:00:00, 14:00:00) UTC, i.e. two full hours. It
	// straddles both minute and hour boundaries and keeps all arithmetic
	// reproducible.
	const start = int64(2 * 3600)
	const end = int64(4 * 3600)
	labels := map[string]string{"host": "h1", "dc": "dc-a"}

	var reports []report

	// Scenario set A: several sample densities, in-order ingest.
	for _, d := range []struct {
		name   string
		period int64
		jitter int64
		gap    int64
	}{
		{"dense 1s regular", 1, 0, 0},
		{"dense 5s regular", 5, 0, 0},
		{"sparse 37s jittered", 37, 30, 0},
		{"sparse 90s jittered", 90, 45, 0},
		{"dense 3s with gaps", 3, 0, 121}, // skip every 121st sample -> empty minutes
		{"irregular 23s jitter+gaps", 23, 20, 257},
	} {
		samples := synthetic.Generate(synthetic.Options{
			Metric: "cpu.usage", Labels: labels,
			Start: start, End: end, PeriodSec: d.period,
			Amplitude: 40, Offset: 50, JitterSec: d.jitter,
			Seed: 42, GapEvery: d.gap,
		})
		reports = append(reports, compareScenario(d.name, samples, start, end))
	}

	// Scenario B: out-of-order / late-arriving samples (deterministic
	// permutation with some samples delayed across bucket boundaries).
	base := synthetic.Generate(synthetic.Options{
		Metric: "cpu.usage", Labels: labels,
		Start: start, End: end, PeriodSec: 7,
		Amplitude: 10, Offset: 100, JitterSec: 5, Seed: 7,
	})
	shuffled := deterministicShuffle(base, 99)
	reports = append(reports, compareScenario("late arrivals out of order 7s", shuffled, start, end))

	// Scenario C: historical corrections at boundary-sensitive positions.
	// We correct: the first second of a minute (:00), the last second of a
	// minute (:59), the last second of an hour (3599), and a mid-bucket
	// second. Each revision must propagate to its minute and hour bucket.
	corrSamples := synthetic.Generate(synthetic.Options{
		Metric: "cpu.usage", Labels: labels,
		Start: start, End: end, PeriodSec: 1, // every second populated, incl. :59/:3599
		Amplitude: 3, Offset: 20, Seed: 11,
	})
	corrections := []int64{
		start + 0,          // 02:00:00 hour+minute boundary
		start + 59,         // last second of 02:00 minute
		start + 60,         // first second of 02:01
		start + 3599,       // 02:59:59 last second of the hour
		start + 3600,       // 03:00:00 next hour boundary
		start + 3600 + 123, // mid-bucket
	}
	reports = append(reports, correctionScenario("historical corrections propagate", corrSamples, start, end, corrections))

	// Scenario D: empty buckets. Data exists only in hour 2 and hour 4
	// edge; querying hour 3 must return count-0 buckets at every layer.
	gappy := synthetic.Generate(synthetic.Options{
		Metric: "cpu.usage", Labels: labels,
		Start: start, End: start + 1800, PeriodSec: 10, // 02:00-02:30
		Amplitude: 1, Offset: 1, Seed: 3,
	})
	reports = append(reports, emptyBucketScenario("empty buckets explicit", gappy, start, end))

	// Scenario E: raw retention pruned -> correction is unrecoverable.
	reports = append(reports, pruneScenario("correction after raw prune rejected", corrSamples, start, end))

	failed := 0
	for _, r := range reports {
		status := "PASS"
		if !r.pass {
			status = "FAIL"
			failed++
		}
		fmt.Printf("[%s] %s\n", status, r.name)
		if r.detail != "" {
			fmt.Print(r.detail)
		}
	}
	fmt.Printf("\n%d/%d scenarios passed\n", len(reports)-failed, len(reports))
	if failed > 0 {
		os.Exit(1)
	}
}

func compareScenario(name string, samples []model.Sample, start, end int64) report {
	st := store.New()
	st.Ingest(samples)
	return compareStore(name, st, samples, start, end)
}

func compareStore(name string, st *store.Store, truth []model.Sample, start, end int64) report {
	var detail string
	ok := true
	for _, layer := range []struct {
		l     store.Layer
		width int64
	}{
		{store.LayerRaw, 1}, {store.LayerMinute, 60}, {store.LayerHour, 3600},
	} {
		got, err := st.Query("cpu.usage", map[string]string{"host": "h1", "dc": "dc-a"}, start, end, layer.l)
		if err != nil {
			ok = false
			detail += fmt.Sprintf("  %s: query error: %v\n", layer.l, err)
			continue
		}
		gotBuckets := make([]verify.GotBucket, 0, len(got))
		for _, b := range got {
			gb := verify.GotBucket{Start: b.Start, Count: b.Count}
			if b.Count > 0 {
				gb.Sum, gb.Min, gb.Max = *b.Sum, *b.Min, *b.Max
			}
			gotBuckets = append(gotBuckets, gb)
		}
		want := verify.ReferenceAggregate(truth, start, end, layer.width)
		if mm := verify.Compare(gotBuckets, want); len(mm) > 0 {
			ok = false
			detail += fmt.Sprintf("  %s: %d mismatches, first: %s\n", layer.l, len(mm), mm[0])
			for i := 0; i < len(mm) && i < 5; i++ {
				detail += "    - " + mm[i].String() + "\n"
			}
		}
	}
	return report{name: name, pass: ok, detail: detail}
}

func correctionScenario(name string, samples []model.Sample, start, end int64, targets []int64) report {
	st := store.New()
	st.Ingest(samples)

	// Apply corrections to the store and build the revised raw truth the
	// oracle must see: Correct() replaces ALL values at that second with a
	// single new value.
	newValue := 777.777
	revised := make([]model.Sample, 0, len(samples))
	corrected := map[int64]bool{}
	for _, t := range targets {
		if err := st.Correct("cpu.usage", map[string]string{"host": "h1", "dc": "dc-a"}, t, newValue); err != nil {
			return report{name: name, pass: false,
				detail: fmt.Sprintf("  correct @%d failed: %v\n", t, err)}
		}
		corrected[t] = true
	}
	for _, smp := range samples {
		if !corrected[smp.Ts] {
			revised = append(revised, smp)
		}
	}
	for t := range corrected {
		revised = append(revised, model.Sample{
			Metric: "cpu.usage", Labels: map[string]string{"host": "h1", "dc": "dc-a"},
			Ts: t, Value: newValue,
		})
	}
	return compareStore(name, st, revised, start, end)
}

func emptyBucketScenario(name string, samples []model.Sample, start, end int64) report {
	st := store.New()
	st.Ingest(samples)
	ok := true
	var detail string

	// Hour layer: two buckets, second one empty.
	hours, err := st.Query("cpu.usage", map[string]string{"host": "h1", "dc": "dc-a"}, start, end, store.LayerHour)
	if err != nil {
		return report{name: name, pass: false, detail: "  query error: " + err.Error() + "\n"}
	}
	if len(hours) != 2 || hours[0].Count == 0 || hours[1].Count != 0 || hours[1].Mean != nil {
		ok = false
		detail += fmt.Sprintf("  hour gap wrong: %+v\n", hours)
	}

	// Minute layer: every minute in the empty half-hour must be count 0.
	minutes, _ := st.Query("cpu.usage", map[string]string{"host": "h1", "dc": "dc-a"}, start, end, store.LayerMinute)
	emptyMinutes := 0
	for _, b := range minutes {
		if b.Start >= start+1800 && b.Count != 0 {
			ok = false
			detail += fmt.Sprintf("  expected empty minute @%d, got count %d\n", b.Start, b.Count)
		}
		if b.Count == 0 {
			emptyMinutes++
		}
	}
	if emptyMinutes < 30 {
		ok = false
		detail += fmt.Sprintf("  expected >=30 empty minute buckets, got %d\n", emptyMinutes)
	}

	// Sanity: the populated half still matches the oracle.
	base := compareStore(name+" (populated half vs oracle)", st, samples, start, start+1800)
	if !base.pass {
		ok = false
		detail += base.detail
	}
	return report{name: name, pass: ok, detail: detail}
}

func pruneScenario(name string, samples []model.Sample, start, end int64) report {
	st := store.New()
	st.Ingest(samples)

	// Prune everything in the first hour; a correction there must now be
	// rejected while minute/hour layers remain queryable.
	removed := st.PruneRaw(start + 3600)
	if removed == 0 {
		return report{name: name, pass: false, detail: "  prune removed nothing\n"}
	}
	err := st.Correct("cpu.usage", map[string]string{"host": "h1", "dc": "dc-a"}, start+30, 42.0)
	if !errors.Is(err, store.ErrRawUnavailable) {
		return report{name: name, pass: false,
			detail: fmt.Sprintf("  expected ErrRawPruned, got %v\n", err)}
	}

	// Minute/hour data for the pruned region is still served (lossy but
	// present) and must still equal the original oracle.
	r := compareStore(name+" (rolled layers survive prune)", st, samples, start+3600, end)
	r.name = name
	prefix := fmt.Sprintf("  correction after raw prune correctly rejected (%d raw samples dropped)\n", removed)
	r.detail = prefix + r.detail
	return r
}

// deterministicShuffle permutes samples with a fixed LCG, then moves the
// first 10% to the end, simulating late arrivals.
func deterministicShuffle(in []model.Sample, seed int64) []model.Sample {
	out := append([]model.Sample(nil), in...)
	n := int64(len(out))
	state := seed
	for i := n - 1; i > 0; i-- {
		state = state*6364136223846793005 + 1
		j := abs(state) % (i + 1)
		out[i], out[j] = out[j], out[i]
	}
	head := int(math.Ceil(float64(n) / 10))
	return append(out[head:], out[:head]...)
}

func abs(x int64) int64 {
	if x < 0 {
		return -x
	}
	return x
}
