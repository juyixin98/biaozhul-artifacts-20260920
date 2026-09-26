package fault

import (
	"testing"
	"time"
)

func TestScriptPlaysPerRequest(t *testing.T) {
	s := NewScript(
		Unavailable(0),
		Unavailable(0),
		OK(),
	)
	// Request A replays the same sequence independently of B.
	for req, wantLast := range map[string]int{"A": 200, "B": 200} {
		var lastStatus int
		for i := 0; i < 3; i++ {
			out, hit := s.Next(req)
			if hit != i+1 {
				t.Fatalf("hit=%d, want %d", hit, i+1)
			}
			lastStatus = out.Status
		}
		if lastStatus != wantLast {
			t.Fatalf("req %s final status=%d, want %d", req, lastStatus, wantLast)
		}
	}
	if s.Hits("A") != 3 || s.Hits("B") != 3 {
		t.Fatalf("hits A=%d B=%d, want 3/3", s.Hits("A"), s.Hits("B"))
	}
}

func TestScriptRepeatsLastOutcome(t *testing.T) {
	s := NewScript(Unavailable(0))
	for i := 0; i < 5; i++ {
		if out, _ := s.Next("r"); out.Status != 503 {
			t.Fatalf("call %d status=%d, want repeated 503", i, out.Status)
		}
	}
}

func TestEmptyScriptIsOK(t *testing.T) {
	s := NewScript()
	if out, _ := s.Next("r"); out.Status != 200 {
		t.Fatalf("status=%d, want 200", out.Status)
	}
}

func TestTooManyRequestsCarriesHint(t *testing.T) {
	out := TooManyRequests(2 * time.Second)
	if out.Status != 429 || out.RetryAfter != 2*time.Second {
		t.Fatalf("outcome=%+v", out)
	}
}
