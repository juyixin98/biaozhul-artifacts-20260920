package sched

import (
	"math/rand"
	"testing"
	"time"

	"rsrv/brute"
)

// TestDifferentialBatch compares whole atomic batches against a brute-force
// simulation that places items one by one on the discrete-hour grid. It checks
// both the commit decision and every planned slot, and re-verifies that a
// rejected batch leaves zero reservations behind.
func TestDifferentialBatch(t *testing.T) {
	const iterations = 2000
	rng := rand.New(rand.NewSource(424242))
	dimNames := []string{"c1", "c2"}

	for iter := 0; iter < iterations; iter++ {
		capV := map[string]int64{}
		capD := Dims{}
		for _, d := range dimNames {
			v := int64(rng.Intn(3))
			capV[d] = v
			capD[d] = v
		}
		// Committed jobs from prior batches.
		var prior []brute.Job
		sch := New(WithClock(NewFakeClock(hour(100))))
		if err := sch.AddResource(&Resource{ID: "R", Capacity: capD}); err != nil {
			t.Fatal(err)
		}
		nPrior := rng.Intn(5)
		var priorItems []BatchItem
		for i := 0; i < nPrior; i++ {
			a := int64(rng.Intn(30))
			b := a + int64(1+rng.Intn(4))
			dem := map[string]int64{dimNames[rng.Intn(len(dimNames))]: int64(1 + rng.Intn(2))}
			prior = append(prior, brute.Job{ID: "p" + itoa(i), Start: a, End: b, Demand: dem})
			priorItems = append(priorItems, fixed("p"+itoa(i), "R", a, b, Dims(dem)))
		}
		if len(priorItems) > 0 {
			if _, err := sch.CommitBatch(&BatchRequest{ID: "prior", Items: priorItems}); err != nil {
				// Prior batch itself may be infeasible; the brute list would
				// then not represent the store. Skip these draws.
				continue
			}
		}

		// Candidate batch.
		nItems := 1 + rng.Intn(4)
		var items []BatchItem
		type spec struct {
			kind                    string
			start, end, ws, we, dur int64
			dem                     map[string]int64
		}
		var specs []spec
		for i := 0; i < nItems; i++ {
			dem := map[string]int64{}
			for _, d := range dimNames {
				if rng.Intn(2) == 0 {
					dem[d] = int64(1 + rng.Intn(2))
				}
			}
			if len(dem) == 0 {
				dem[dimNames[0]] = 1
			}
			dur := int64(1 + rng.Intn(3))
			ws := int64(rng.Intn(30))
			we := ws + dur + int64(rng.Intn(8))
			var sp spec
			sp.dem = dem
			sp.dur = dur
			sp.ws = ws
			sp.we = we
			if rng.Intn(2) == 0 {
				sp.kind = "fixed"
				lo := ws
				hi := we - dur
				if hi < lo {
					hi = lo
				}
				s := lo + int64(rng.Intn(int(hi-lo+1)))
				sp.start = s
				sp.end = s + dur
				items = append(items, fixed("i"+itoa(i), "R", sp.start, sp.end, Dims(dem)))
			} else {
				sp.kind = "earliest"
				items = append(items, earliest("i"+itoa(i), "R",
					time.Duration(dur)*time.Hour, ws, we, Dims(dem)))
			}
			specs = append(specs, sp)
		}

		// Brute simulation: sequential placement on a copy of the prior grid.
		sim := append([]brute.Job{}, prior...)
		bruteCommit := true
		var bruteStarts []int64
		for _, sp := range specs {
			if sp.kind == "fixed" {
				if !brute.FixedFits(capV, sim, sp.start, sp.end-sp.start, sp.dem) {
					bruteCommit = false
					break
				}
				bruteStarts = append(bruteStarts, sp.start)
				sim = append(sim, brute.Job{Start: sp.start, End: sp.end, Demand: sp.dem})
			} else {
				s, ok := brute.EarliestFeasibleGrid(capV, sim, sp.ws, sp.we, sp.dur, sp.dem)
				if !ok {
					bruteCommit = false
					break
				}
				bruteStarts = append(bruteStarts, s)
				sim = append(sim, brute.Job{Start: s, End: s + sp.dur, Demand: sp.dem})
			}
		}

		before := len(sch.Reservations("R"))
		batch, err := sch.CommitBatch(&BatchRequest{ID: "cand", Items: items})
		after := len(sch.Reservations("R"))
		gotCommit := err == nil

		if gotCommit != bruteCommit {
			t.Fatalf("iter %d: commit decision mismatch brute=%v sched=%v err=%v\ncap=%v prior=%+v specs=%+v",
				iter, bruteCommit, gotCommit, err, capV, prior, specs)
		}
		if !gotCommit {
			if after != before {
				t.Fatalf("iter %d: rejected batch partially landed: %d -> %d", iter, before, after)
			}
			if _, exists := sch.GetBatch("cand"); exists {
				t.Fatalf("iter %d: rejected batch stored", iter)
			}
			continue
		}
		if after-before != nItems {
			t.Fatalf("iter %d: committed %d reservations, want %d", iter, after-before, nItems)
		}
		for i, want := range bruteStarts {
			if !batch.Planned[i].Start.Equal(hour(want)) {
				t.Fatalf("iter %d item %d: planned start %v, brute %d\ncap=%v prior=%+v specs=%+v",
					iter, i, batch.Planned[i].Start, want, capV, prior, specs)
			}
		}
	}
}
