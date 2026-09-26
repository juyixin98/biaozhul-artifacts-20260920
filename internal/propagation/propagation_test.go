package propagation

import (
	"net/http"
	"testing"
	"time"
)

func TestInjectAndParseRoundTrip(t *testing.T) {
	dl := time.Unix(1_700_000_000, 123).UTC()
	h := http.Header{}
	Inject(h, "abc-123", dl, 7, 3)

	got, err := Parse(h)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if !got.HasBudget || got.RequestID != "abc-123" || got.Max != 7 || got.Used != 3 {
		t.Fatalf("parsed = %+v", got)
	}
	if !got.Deadline.Equal(dl) {
		t.Fatalf("deadline = %v, want %v", got.Deadline, dl)
	}
	if r := got.Remaining(); r != 4 {
		t.Fatalf("Remaining = %d, want 4", r)
	}
}

func TestParseAbsentBudget(t *testing.T) {
	got, err := Parse(http.Header{})
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if got.HasBudget {
		t.Fatal("empty headers must not report a budget")
	}
}

func TestParseMalformed(t *testing.T) {
	cases := map[string]http.Header{
		"bad max":      {HeaderMax: []string{"x"}},
		"negative max": {HeaderMax: []string{"-1"}},
		"bad used":     {HeaderMax: []string{"5"}, HeaderUsed: []string{"nope"}},
		"bad deadline": {HeaderMax: []string{"5"}, HeaderDeadline: []string{"not-a-time"}},
	}
	for name, h := range cases {
		if _, err := Parse(h); err == nil {
			t.Fatalf("%s: expected parse error", name)
		}
	}
}

func TestRemainingClampsAtZero(t *testing.T) {
	h := http.Header{}
	Inject(h, "r", time.Time{}, 3, 9)
	got, _ := Parse(h)
	if r := got.Remaining(); r != 0 {
		t.Fatalf("Remaining = %d, want 0 (clamped)", r)
	}
}
