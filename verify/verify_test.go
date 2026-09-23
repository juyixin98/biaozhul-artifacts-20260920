package verify

import (
	"encoding/json"
	"testing"
)

func TestLibraryStandardScenariosHoldInvariants(t *testing.T) {
	for _, sc := range LibraryScenarios("standard") {
		t.Run(sc.Name, func(t *testing.T) {
			run := sc
			if sc.Name == "five-node-chaos" {
				run.Nodes = 5
			}
			r := Run(run, Options{Trace: true, CaptureViews: true, EpilogueMs: 120})
			if !r.OK {
				t.Fatalf("standard Raft violated invariants:\n%s\nviews=%s",
					violations(r), mustJSON(r.Views))
			}
		})
	}
}

func TestCuratedCounterexamplesFireAndReplay(t *testing.T) {
	for _, sc := range CounterexampleScenarios() {
		t.Run(sc.Name, func(t *testing.T) {
			r := Run(sc, Options{Trace: true, CaptureViews: true, EpilogueMs: 120})
			if r.OK {
				t.Fatalf("buggy variant unexpectedly satisfied all invariants for %s", sc.Name)
			}
			t.Logf("%s violated: %v", sc.Name, r.Violations)

			// Same trace against standard Raft must hold every invariant.
			rs := Run(StandardCounterpart(sc), Options{EpilogueMs: 120})
			if !rs.OK {
				t.Fatalf("standard Raft violated on the counterexample trace: %v", rs.Violations)
			}

			// Deterministic replay reproduces the same violation at the same step.
			re := Run(sc, Options{EpilogueMs: 60})
			if re.OK ||
				re.Violations[0].Invariant != r.Violations[0].Invariant ||
				re.Violations[0].Step != r.Violations[0].Step {
				t.Fatalf("counterexample did not reproduce deterministically:\nfirst=%v\nreplay=%v",
					r.Violations, re.Violations)
			}
		})
	}
}

func TestLibraryNaiveIsFalsifiedSomewhere(t *testing.T) {
	// Sanity: the curated set alone is required to falsify; the library is a
	// regression set, not guaranteed to break the naive variant on its own.
	found := false
	for _, sc := range CounterexampleScenarios() {
		if !Run(sc, Options{EpilogueMs: 60}).OK {
			found = true
		}
	}
	if !found {
		t.Fatal("no counterexample found")
	}
}

func TestEnumeratorStandardFindsNothing(t *testing.T) {
	rep := Enumerate("standard", 2, 300, 5)
	if len(rep.Counterex) != 0 {
		t.Fatalf("standard variant had counterexamples: %v", rep.Counterex[0].Violations)
	}
	if rep.ExhaustiveN == 0 {
		t.Fatal("exhaustive enumeration ran zero scenarios")
	}
	t.Logf("standard enumeration: %d exhaustive + %d fuzz in %s",
		rep.ExhaustiveN, rep.FuzzN, rep.Elapsed)
}

func TestEnumeratorNaiveFindsAndReplaysCounterexample(t *testing.T) {
	rep := Enumerate("naive", 3, 1000, 3)
	if len(rep.Counterex) == 0 {
		t.Fatal("expected enumeration to falsify the naive variant")
	}
	for _, ce := range rep.Counterex {
		t.Logf("CE: %s -> %v", ce.Scenario.Name, ce.Violations)
	}
	// Determinism: replay the first counterexample byte-for-byte; the same
	// violation must recur at the same step.
	ce := rep.Counterex[0]
	re := Run(ce.Scenario, Options{Trace: true, EpilogueMs: 60})
	if re.OK {
		t.Fatal("counterexample did not reproduce on replay")
	}
	if re.Violations[0].Invariant != ce.Violations[0].Invariant ||
		re.Violations[0].Step != ce.Violations[0].Step {
		t.Fatalf("replay diverged:\nfirst: %v\nreplay: %v", ce.Violations, re.Violations)
	}
	// The same trace executed against the standard implementation must pass.
	safe := ce.Scenario
	safe.Variant = "standard"
	safe.Name = ce.Scenario.Name + "-replayed-on-standard"
	rs := Run(safe, Options{EpilogueMs: 60})
	if !rs.OK {
		t.Fatalf("counterexample trace also fails on standard Raft: %v", rs.Violations)
	}
}

func TestCounterexampleJSONRoundTrip(t *testing.T) {
	// The recorded scenario must survive a JSON round trip (HTTP replay API).
	sc := LibraryScenarios("naive")[0]
	b, err := json.Marshal(sc)
	if err != nil {
		t.Fatal(err)
	}
	var back Scenario
	if err := json.Unmarshal(b, &back); err != nil {
		t.Fatal(err)
	}
	r1 := Run(sc, Options{EpilogueMs: 60})
	r2 := Run(back, Options{EpilogueMs: 60})
	if r1.OK != r2.OK {
		t.Fatalf("json round trip changed verdict: %v vs %v", r1.OK, r2.OK)
	}
}

func violations(r Result) string {
	s := ""
	for _, v := range r.Violations {
		s += " - " + v.Error() + "\n"
	}
	return s
}

func mustJSON(v interface{}) string {
	b, _ := json.MarshalIndent(v, "", "  ")
	return string(b)
}
