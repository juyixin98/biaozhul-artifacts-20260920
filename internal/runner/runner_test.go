package runner_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"fencinglease/internal/runner"
	"fencinglease/internal/sim"
)

func mustLoad(t *testing.T, path string) *runner.Scenario {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	sc, err := runner.Parse(raw, path)
	if err != nil {
		t.Fatal(err)
	}
	return sc
}

func TestExampleScenarios(t *testing.T) {
	cases := []struct {
		file string
		pass bool
	}{
		{"renew-lost.json", true},
		{"pause-expired.json", true},
		{"late-message.json", true},
		{"duplicate.json", true},
		{"fuzz.json", true},
		{"no-fence.json", false}, // deliberately violates the invariant
	}
	for _, tc := range cases {
		t.Run(tc.file, func(t *testing.T) {
			sc := mustLoad(t, filepath.Join("..", "..", "examples", tc.file))
			res := runner.Run(sc)
			if res.OK != tc.pass {
				var fails []string
				for _, c := range res.Checks {
					if !c.Pass {
						fails = append(fails, c.Kind+": "+c.Detail)
					}
				}
				t.Fatalf("OK=%v want %v; failures:\n%s", res.OK, tc.pass, strings.Join(fails, "\n"))
			}
		})
	}
}

// TestStaleFenceNeverAccepted is the headline acceptance property across all
// three required fault shapes: in every successful run each resource's
// accepted fence tokens are strictly increasing.
func TestStaleFenceNeverAccepted(t *testing.T) {
	for _, f := range []string{"renew-lost.json", "pause-expired.json", "late-message.json", "fuzz.json"} {
		t.Run(f, func(t *testing.T) {
			sc := mustLoad(t, filepath.Join("..", "..", "examples", f))
			res := runner.Run(sc)
			seen := map[string]int64{}
			for _, c := range res.Commits {
				if prev, ok := seen[c.Resource]; ok && c.Fence <= prev {
					t.Fatalf("resource %s accepted fence %d after %d (node %s)", c.Resource, c.Fence, prev, c.Node)
				}
				seen[c.Resource] = c.Fence
			}
		})
	}
}

// TestFuzzManySeeds reruns the lossy shape over many seeds: whatever the loss,
// duplication, reordering or pause pattern, the fence ordering invariant must
// never break.
func TestFuzzManySeeds(t *testing.T) {
	sc := mustLoad(t, filepath.Join("..", "..", "examples", "fuzz.json"))
	for seed := int64(1); seed <= 25; seed++ {
		sc.Seed = seed
		res := runner.Run(sc)
		seen := map[string]int64{}
		for _, c := range res.Commits {
			if prev, ok := seen[c.Resource]; ok && c.Fence <= prev {
				t.Fatalf("seed %d: resource %s accepted fence %d after %d", seed, c.Resource, c.Fence, prev)
			}
			seen[c.Resource] = c.Fence
		}
		// Sanity: an empty commit log would make the test vacuous.
		if len(res.Commits) == 0 {
			t.Fatalf("seed %d: no commits accepted at all — workload not exercising writes", seed)
		}
	}
}

// TestEndToEndPauseScenario independently constructs the pause-past-lease
// scenario in code (not via JSON) to pin the semantics.
func TestEndToEndPauseScenario(t *testing.T) {
	sc := &runner.Scenario{
		Name:      "inline",
		Seed:      1,
		MaxTime:   60,
		Network:   sim.NetConfig{MinDelay: 1},
		Resources: []runner.ResourceCfg{{Name: "x", NodeID: "rx", Fenced: true}},
		Nodes:     []string{"A", "B"},
		Actions: []runner.Action{
			{Time: 0, Type: "acquire", Node: "A", Resource: "x", TTL: 10},
			{Time: 4, Type: "pause", Node: "A", Until: 22},
			{Time: 12, Type: "acquire", Node: "B", Resource: "x", TTL: 10},
			{Time: 16, Type: "submit", Node: "B", Resource: "x", Value: "b"},
			{Time: 24, Type: "submit", Node: "A", Resource: "x", Value: "a-stale"},
		},
	}
	res := runner.Run(sc)
	if !res.OK {
		t.Fatalf("inline scenario should pass: %+v", res.Checks)
	}
	if len(res.Commits) != 1 || res.Commits[0].Node != "B" || res.Commits[0].Fence != 2 {
		t.Fatalf("only B/fence-2 may commit, got %+v", res.Commits)
	}
}
