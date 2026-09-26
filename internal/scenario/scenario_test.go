package scenario_test

import (
	"testing"

	"cbhalfopen/internal/scenario"
)

// TestAllScenariosPass is the deterministic regression suite for the four
// acceptance stories. Scenarios park real goroutines on channels but never wait on
// wall-clock time, so the test is fast and repeatable.
func TestAllScenariosPass(t *testing.T) {
	for _, d := range scenario.All() {
		t.Run(d.Name, func(t *testing.T) {
			r := d.Run()
			if !r.Pass {
				for _, s := range r.Steps {
					for _, c := range s.Checks {
						if !c.Pass {
							t.Errorf("step %q check %q failed: %s", s.Name, c.Name, c.Detail)
						}
					}
				}
			}
		})
	}
}

// TestScenarioReportsArePopulated guards the structured report shape consumers
// (the demo CLI and the JSON report) rely on.
func TestScenarioReportsArePopulated(t *testing.T) {
	for _, d := range scenario.All() {
		t.Run(d.Name, func(t *testing.T) {
			r := d.Run()
			if r.Name == "" || r.Goal == "" {
				t.Fatal("name/goal missing")
			}
			if len(r.Steps) < 3 {
				t.Fatalf("only %d steps", len(r.Steps))
			}
			checks := 0
			for _, s := range r.Steps {
				if s.Snapshot == nil {
					t.Fatalf("step %q has no snapshot", s.Name)
				}
				checks += len(s.Checks)
			}
			if checks == 0 {
				t.Fatal("report contains no checks")
			}
			for _, key := range []string{
				"allowed", "rejected", "successes", "failures",
				"canceled", "probes_granted", "probes_rejected", "stale_results",
			} {
				if _, ok := r.Summary[key]; !ok {
					t.Fatalf("summary missing counter %q", key)
				}
			}
		})
	}
}
