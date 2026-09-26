package main

import (
	"net/http/httptest"
	"testing"

	"github.com/example/rangeserver/internal/server"
)

// TestAllAcceptanceScenariosPass runs the exact same scenario set as the
// `verify` command against an in-process fake server.
func TestAllAcceptanceScenariosPass(t *testing.T) {
	ts := httptest.NewServer(server.New(seedArtifacts()))
	t.Cleanup(ts.Close)

	h := &harness{baseURL: ts.URL, raw: ts.Client()}
	results := h.runAll()
	if len(results) != 14 {
		t.Fatalf("scenarios = %d, want 14", len(results))
	}
	for _, r := range results {
		if !r.Passed {
			t.Errorf("scenario %q failed: %s (error=%v)", r.Name, r.Expectation, r.Error)
		}
	}
}
