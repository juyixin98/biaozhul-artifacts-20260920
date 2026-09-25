package sampler

import "testing"

func TestEvaluatePoliciesError(t *testing.T) {
	cfg := testConfig()
	keep, reasons := evaluatePolicies([]Span{
		mkSpan("t", "r", "", "ok", 0, 10),
		mkSpan("t", "c", "r", "error", 5, 3),
	}, cfg)
	if !keep || !contains(reasons, "error_span") {
		t.Fatalf("keep=%v reasons=%v", keep, reasons)
	}
}

func TestEvaluatePoliciesLatency(t *testing.T) {
	cfg := testConfig()
	keep, reasons := evaluatePolicies([]Span{
		mkSpan("t", "r", "", "ok", 0, 600),
	}, cfg)
	if !keep || len(reasons) != 1 || reasons[0] != "latency_exceeded(600ms>=500ms)" {
		t.Fatalf("keep=%v reasons=%v", keep, reasons)
	}
}

func TestEvaluatePoliciesNone(t *testing.T) {
	cfg := testConfig()
	keep, reasons := evaluatePolicies([]Span{
		mkSpan("t", "r", "", "ok", 0, 10),
	}, cfg)
	if keep || len(reasons) != 0 {
		t.Fatalf("keep=%v reasons=%v", keep, reasons)
	}
}

func TestCompleteness(t *testing.T) {
	complete := completeness([]Span{
		mkSpan("t", "r", "", "ok", 0, 10),
		mkSpan("t", "c", "r", "ok", 0, 5),
	})
	if len(complete) != 0 {
		t.Fatalf("expected complete, got %v", complete)
	}
	noRoot := completeness([]Span{mkSpan("t", "c", "r", "ok", 0, 5)})
	if !contains(noRoot, "root_span_missing") || !contains(noRoot, "parent_spans_missing(1)") {
		t.Fatalf("got %v", noRoot)
	}
}
