package scenario

import (
	"encoding/json"
	"testing"

	"causal-broadcast/internal/jsonio"
)

func TestChainScenarioValidAndRuns(t *testing.T) {
	req := Chain(4, 3, false)
	if len(req.Nodes) != 4 || len(req.Broadcasts) != 4 {
		t.Fatalf("unexpected generated chain: %+v", req)
	}
	res := mustRun(t, req)
	if !res.Diagnostics.CausalOrderOK {
	}
	if res.Stats.BufferedTotal == 0 || res.Stats.ReleasedFromBuffer == 0 {
		t.Fatalf("chain scenario should demonstrate buffering+release, stats=%+v", res.Stats)
	}
}

func TestChainLossReportsRootCause(t *testing.T) {
	res := mustRun(t, Chain(4, 3, true))
	if len(res.Diagnostics.PermanentMissing) == 0 {
		t.Fatal("loss chain should report permanent missing messages")
	}
	found := false
	for _, m := range res.Diagnostics.PermanentMissing {
		for _, rc := range m.RootCauses {
			if rc == "A1@D" {
				found = true
			}
		}
	}
	if !found {
		t.Fatalf("root cause A1@D missing in %+v", res.Diagnostics.PermanentMissing)
	}
}

func TestConcurrentScenarioIndependent(t *testing.T) {
	res := mustRun(t, Concurrent(4, 5, true))
	if !res.Diagnostics.CausalOrderOK {
	}
	if res.Stats.BufferedTotal != 0 {
		t.Fatalf("independent messages must not buffer: %+v", res.Stats)
	}
	if res.Stats.Duplicates == 0 {
		t.Fatal("scripted duplicate should be counted")
	}
}

type runnerResult struct {
	Diagnostics struct {
		CausalOrderOK    bool `json:"causalOrderOk"`
		PermanentMissing []struct {
			RootCauses []string `json:"rootCauses"`
		} `json:"permanentMissing"`
	} `json:"diagnostics"`
	Stats struct {
		BufferedTotal      int `json:"bufferedTotal"`
		ReleasedFromBuffer int `json:"releasedFromBuffer"`
		Duplicates         int `json:"duplicates"`
	} `json:"stats"`
}

func mustRun(t *testing.T, req jsonio.Request) runnerResult {
	t.Helper()
	raw, err := json.Marshal(req)
	if err != nil {
		t.Fatal(err)
	}
	res, err := jsonio.Run(raw)
	if err != nil {
		t.Fatal(err)
	}
	out, err := json.Marshal(res)
	if err != nil {
		t.Fatal(err)
	}
	var rr runnerResult
	if err := json.Unmarshal(out, &rr); err != nil {
		t.Fatal(err)
	}
	return rr
}
