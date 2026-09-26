package demo

import "testing"

func TestRunSuiteAllPass(t *testing.T) {
	results := RunSuite()
	if len(results) == 0 {
		t.Fatal("suite is empty")
	}
	for _, r := range results {
		if !r.Passed {
			t.Errorf("case %s failed: %v", r.Case, r.Reasons)
		}
		// Every acceptance case must satisfy the anti-amplification bound.
		if r.Outcome.TotalUsed > r.Outcome.RootMax {
			t.Errorf("case %s used %d > root max %d", r.Case,
				r.Outcome.TotalUsed, r.Outcome.RootMax)
		}
	}
}

func TestSuiteCoversRequiredAcceptanceBehaviors(t *testing.T) {
	want := map[string]bool{
		"budget-exhausted":  false,
		"deadline-exceeded": false,
		"canceled":          false,
		"success":           false,
		"non-retryable":     false,
	}
	for _, r := range RunSuite() {
		if _, ok := want[r.Outcome.Status]; ok {
			want[r.Outcome.Status] = true
		}
	}
	for status, seen := range want {
		if !seen {
			t.Errorf("required terminal status not exercised by suite: %s", status)
		}
	}
}
