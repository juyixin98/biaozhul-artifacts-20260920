// Command verify runs the acceptance scenarios entirely in-process: it
// starts the range service backed by in-memory fakes, drives it with the
// fault-injecting client and raw HTTP requests, and emits a structured
// JSON report of every scenario. It never talks to a production system.
//
// Exit code is 0 only when every scenario passed; the JSON records the
// exact commands-equivalent requests, responses, and any failures.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"time"

	"github.com/example/rangeserver/internal/artifact"
	"github.com/example/rangeserver/internal/client"
	"github.com/example/rangeserver/internal/clock"
	"github.com/example/rangeserver/internal/fault"
	"github.com/example/rangeserver/internal/server"
)

// ScenarioResult is one structured acceptance result.
type ScenarioResult struct {
	Name        string         `json:"name"`
	Passed      bool           `json:"passed"`
	Expectation string         `json:"expectation"`
	Detail      map[string]any `json:"detail,omitempty"`
	Report      *client.Report `json:"client_report,omitempty"`
	Error       string         `json:"error,omitempty"`
}

func main() {
	pretty := flag.Bool("pretty", true, "indent the JSON report")
	flag.Parse()

	reg := seedArtifacts()
	ts := httptest.NewServer(server.New(reg))
	defer ts.Close()

	runner := &harness{
		baseURL: ts.URL,
		raw:     ts.Client(),
	}
	results := runner.runAll()

	failed := 0
	for _, r := range results {
		if !r.Passed {
			failed++
		}
	}
	report := map[string]any{
		"generated_at":     time.Now().UTC().Format(time.RFC3339),
		"base_url":         ts.URL,
		"total_scenarios":  len(results),
		"passed_scenarios": len(results) - failed,
		"failed_scenarios": failed,
		"all_passed":       failed == 0,
		"scenarios":        results,
	}

	enc := json.NewEncoder(os.Stdout)
	if *pretty {
		enc.SetIndent("", "  ")
	}
	if err := enc.Encode(report); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	if failed > 0 {
		os.Exit(1)
	}
}

// seedArtifacts builds the deterministic corpus shared by all scenarios.
func seedArtifacts() *artifact.Registry {
	reg := artifact.NewRegistry()
	mod := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)

	binary256 := make([]byte, 256)
	for i := range binary256 {
		binary256[i] = byte(i)
	}
	reg.Put(artifact.NewWithMetadata("binary256", binary256, mod))
	reg.Put(artifact.NewWithMetadata("lorem300", []byte(pad("ABCDEFGHIJ", 30)), mod.Add(time.Second)))
	reg.Put(artifact.NewWithMetadata("empty", []byte{}, mod.Add(2*time.Second)))
	return reg
}

func pad(unit string, times int) string {
	out := ""
	for i := 0; i < times; i++ {
		out += unit
	}
	return out
}

// harness holds shared per-run state.
type harness struct {
	baseURL string
	raw     *http.Client
}

func (h *harness) runAll() []ScenarioResult {
	return []ScenarioResult{
		h.prefixRange(),
		h.suffixRange(),
		h.openEndedRange(),
		h.multiRangeTiled(),
		h.overlappingRanges(),
		h.unsatisfiableRange(),
		h.zeroLengthArtifact(),
		h.etagMismatch(),
		h.ifRangeDateForms(),
		h.malformedRange(),
		h.tooManyRanges(),
		h.retryAfterTransportFaults(),
		h.retryAfterTruncatedBody(),
		h.gzipRepresentation(),
	}
}

// faultClient builds a client whose transport scripts the given faults;
// backoff runs against a FakeClock so the suite never really sleeps. A
// background pump advances that fake clock; the returned stop function
// must be called when the scenario is finished with the client.
func (h *harness) faultClient(faults []fault.Fault) (*client.Client, func()) {
	fake := clock.NewFake(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC))
	transport := fault.NewScriptedTransport(http.DefaultTransport, faults)
	hc := &http.Client{Transport: transport}

	pumpStop := make(chan struct{})
	go func() {
		ticker := time.NewTicker(time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				fake.Advance(25 * time.Millisecond)
			case <-pumpStop:
				return
			}
		}
	}()

	c := client.New(client.Config{
		BaseURL:   h.baseURL,
		HTTP:      hc,
		Clock:     fake,
		MaxRanges: server.DefaultMaxRanges,
	})
	return c, func() { close(pumpStop) }
}

func (h *harness) goodClient() *client.Client {
	c, stop := h.faultClient(nil)
	// No faults are scripted, so no retry ever sleeps; stop the (idle)
	// pump on garbage-collection boundary is unnecessary, but keep lifecycle
	// explicit by tying it to a short timer-free path: stop immediately is
	// safe because a passing request never calls Sleep.
	stop()
	return c
}
