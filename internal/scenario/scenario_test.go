package scenario_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"breakerhalfopen/internal/scenario"
)

// TestAcceptanceScenarios runs every built-in scenario with the virtual clock
// and requires all expectations to pass. A structured JSON report for each
// scenario is written to testdata/reports for inspection.
func TestAcceptanceScenarios(t *testing.T) {
	t.Parallel()

	reportDir := filepath.Join("testdata", "reports")
	if err := os.MkdirAll(reportDir, 0o755); err != nil {
		t.Fatalf("mkdir reports: %v", err)
	}

	for _, fn := range scenario.All() {
		rep := fn()
		t.Run(rep.Name, func(t *testing.T) {
			if !rep.Pass {
				for _, f := range rep.Failures {
					t.Errorf("expectation failed: %s", f)
				}
			}
			if t.Failed() {
				return
			}
			// Sanity: the final state of every acceptance story is recovery.
			if rep.Final.State != "closed" {
				t.Fatalf("final state=%s, want closed", rep.Final.State)
			}
		})

		// Persist the structured report regardless of pass/fail.
		if err := writeReport(reportDir, rep.Name, rep); err != nil {
			t.Errorf("write report %s: %v", rep.Name, err)
		}
	}
}

func writeReport(dir, name string, rep scenario.Report) error {
	f, err := os.Create(filepath.Join(dir, name+".json"))
	if err != nil {
		return err
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	return enc.Encode(rep)
}
