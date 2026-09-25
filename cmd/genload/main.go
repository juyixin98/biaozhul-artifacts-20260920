// genload drives the acceptance scenarios against a running tailsampler:
// normal/slow/error traces, a late error span, a large trace, and budget
// exhaustion. It prints the resulting decisions for manual verification.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"time"

	"tailsampler/internal/sampler"
)

var (
	base       = flag.String("base", "http://localhost:8080", "tailsampler base URL")
	wait       = flag.Duration("wait", 12*time.Second, "time to wait for the decision window to close")
	scenario   = flag.String("scenario", "all", "all|basic|late-error|large|budget")
	largeSpans = flag.Int("large-spans", 2000, "span count for the large-trace scenario")
	burst      = flag.Int("burst", 12, "number of error traces sent in the budget scenario")
	runID      = flag.String("run-id", fmt.Sprintf("%d", time.Now().Unix()), "prefix for synthetic trace IDs")
)

func main() {
	flag.Parse()
	fmt.Printf("genload run-id=%s base=%s scenario=%s\n", *runID, *base, *scenario)

	switch *scenario {
	case "basic":
		basic()
	case "late-error":
		lateError()
	case "large":
		large()
	case "budget":
		budget()
	case "all":
		basic()
		lateError()
		large()
		budget()
	default:
		fmt.Println("unknown scenario:", *scenario)
	}
}

func id(parts ...string) string {
	s := *runID
	for _, p := range parts {
		s += "-" + p
	}
	return s
}

func nowMs() int64 { return time.Now().UnixMilli() }

func span(trace, spanID, parent, status string, startMs, durMs int64) sampler.Span {
	return sampler.Span{
		TraceID: trace, SpanID: spanID, ParentID: parent,
		Service: "synthetic", Name: "op-" + spanID,
		StartUnixMs: startMs, DurationMs: durMs, Status: status,
	}
}

func ingest(spans ...sampler.Span) {
	body, _ := json.Marshal(map[string]any{"spans": spans})
	resp, err := http.Post(*base+"/v1/spans", "application/json", bytes.NewReader(body))
	if err != nil {
		fmt.Println("  ingest error:", err)
		return
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	fmt.Printf("  ingest %d span(s) trace=%s -> %s", len(spans), spans[0].TraceID, string(b))
}

func decision(trace string) {
	resp, err := http.Get(*base + "/v1/decisions/" + trace)
	if err != nil {
		fmt.Println("  query error:", err)
		return
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	fmt.Printf("  decision %s -> %s", trace, string(b))
}

// basic: one normal (drop), one slow (keep), one error (keep) trace.
func basic() {
	fmt.Println("[basic] normal / slow / error traces")
	t0 := nowMs()
	ingest(
		span(id("normal"), "r", "", "ok", t0, 20),
		span(id("normal"), "c1", "r", "ok", t0+2, 8),
		span(id("normal"), "c2", "r", "ok", t0+4, 6),
	)
	ingest(
		span(id("slow"), "r", "", "ok", t0, 900),
		span(id("slow"), "c1", "r", "ok", t0+100, 700),
	)
	ingest(
		span(id("error"), "r", "", "ok", t0, 50),
		span(id("error"), "c1", "r", "error", t0+10, 30),
	)
	fmt.Printf("  waiting %s for decisions...\n", *wait)
	time.Sleep(*wait)
	decision(id("normal"))
	decision(id("slow"))
	decision(id("error"))
}

// lateError: trace looks healthy, gets dropped after the window, then an
// error span arrives late. The final decision must stay the original one
// (consistency) and the trace must be flagged incomplete with late_spans>0.
func lateError() {
	fmt.Println("[late-error] error span arrives after the decision")
	t0 := nowMs()
	tr := id("lateerr")
	ingest(
		span(tr, "r", "", "ok", t0, 30),
		span(tr, "c1", "r", "ok", t0+5, 10),
	)
	fmt.Printf("  waiting %s for the drop decision...\n", *wait)
	time.Sleep(*wait)
	decision(tr)
	fmt.Println("  sending the late ERROR span now")
	ingest(span(tr, "c2-late", "r", "error", t0+40, 5))
	time.Sleep(500 * time.Millisecond)
	decision(tr)
}

// large: one trace with many spans, kept via latency policy.
func large() {
	fmt.Printf("[large] one trace with %d spans\n", *largeSpans)
	t0 := nowMs()
	tr := id("large")
	spans := make([]sampler.Span, 0, *largeSpans)
	spans = append(spans, span(tr, "root", "", "ok", t0, 1200))
	for i := 1; i < *largeSpans; i++ {
		spans = append(spans, span(tr, fmt.Sprintf("s%05d", i), "root", "ok", t0+int64(i%1000), 5))
	}
	// Send in chunks of 500.
	for i := 0; i < len(spans); i += 500 {
		end := i + 500
		if end > len(spans) {
			end = len(spans)
		}
		ingest(spans[i:end]...)
	}
	fmt.Printf("  waiting %s for the decision...\n", *wait)
	time.Sleep(*wait)
	decision(tr)
}

// budget: a burst of error traces exceeding the per-minute keep budget;
// the overflow must be dropped with reason budget_exhausted (degraded).
func budget() {
	fmt.Printf("[budget] burst of %d error traces to exhaust the keep budget\n", *burst)
	t0 := nowMs()
	for i := 0; i < *burst; i++ {
		tr := id("budget", fmt.Sprintf("%02d", i))
		ingest(
			span(tr, "r", "", "ok", t0, 40),
			span(tr, "c1", "r", "error", t0+5, 20),
		)
	}
	fmt.Printf("  waiting %s for decisions...\n", *wait)
	time.Sleep(*wait)
	for i := 0; i < *burst; i++ {
		decision(id("budget", fmt.Sprintf("%02d", i)))
	}
	resp, err := http.Get(*base + "/v1/stats")
	if err == nil {
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		fmt.Printf("  stats -> %s", string(b))
	}
}
