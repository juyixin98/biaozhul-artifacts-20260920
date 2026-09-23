package check

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// TestReplayFixtures loads the checked-in Figure 8 trace JSON files and
// replays them, guarding the saved counterexamples against code drift.
func TestReplayFixtures(t *testing.T) {
	cases := []struct {
		file  string
		buggy bool
	}{
		{"figure8-correct.json", false},
		{"figure8-buggy.json", true},
	}
	for _, tc := range cases {
		t.Run(tc.file, func(t *testing.T) {
			b, err := os.ReadFile(filepath.Join("testdata", tc.file))
			if err != nil {
				t.Fatal(err)
			}
			var actions []Action
			if err := json.Unmarshal(b, &actions); err != nil {
				t.Fatal(err)
			}
			_, res := NewRunner(Figure8Config(tc.buggy)).Run(actions)
			if tc.buggy {
				if res.OK() || res.Violation.Kind != ViolationCommittedPrefix ||
					res.Violation.Index != 1 {
					t.Fatalf("buggy fixture lost its violation: %+v", res.Violation)
				}
			} else if !res.OK() {
				t.Fatalf("correct fixture now violates %s: %s",
					res.Violation.Kind, res.Violation.Message)
			}
		})
	}
}
