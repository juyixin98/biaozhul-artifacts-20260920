package retry

import (
	"net/http"
	"testing"
	"time"
)

func TestHeaderRoundTrip(t *testing.T) {
	h := http.Header{}
	SetBudgetHeader(h, 7)
	SetIdempotentHeader(h, true)
	dl := time.UnixMilli(1_700_000_000_000)
	SetDeadlineHeader(h, dl)

	if b := BudgetFromHeader(h); b == nil || b.Remaining() != 7 {
		t.Fatalf("budget header round trip failed: %+v", b)
	}
	if !IdempotentFromHeader(h) {
		t.Fatal("idempotent header round trip failed")
	}
	if got, ok := DeadlineFromHeader(h); !ok || !got.Equal(dl) {
		t.Fatalf("deadline header round trip failed: %v %v", got, ok)
	}
}

func TestHeaderDefaults(t *testing.T) {
	h := http.Header{}
	if BudgetFromHeader(h) != nil {
		t.Fatal("missing budget header should yield nil")
	}
	if IdempotentFromHeader(h) {
		t.Fatal("missing idempotent header must default to false")
	}
	if _, ok := DeadlineFromHeader(h); ok {
		t.Fatal("missing deadline header should not parse")
	}
	h.Set(HeaderBudgetRemaining, "not-a-number")
	if BudgetFromHeader(h) != nil {
		t.Fatal("invalid budget header should yield nil")
	}
	h.Set(HeaderBudgetRemaining, "-3")
	if BudgetFromHeader(h) != nil {
		t.Fatal("negative budget header should yield nil")
	}
}

func TestBudgetStore(t *testing.T) {
	s := NewBudgetStore()
	if s.Get("nope") != nil {
		t.Fatal("unknown id should miss")
	}
	b := NewBudget(3)
	if got := s.Register("id1", b); got != b {
		t.Fatal("first register should store the given budget")
	}
	other := NewBudget(9)
	if got := s.Register("id1", other); got != b {
		t.Fatal("existing entry must win")
	}
	if s.Get("id1") != b {
		t.Fatal("get should return the shared budget")
	}
	s.Release("id1")
	if s.Get("id1") != nil {
		t.Fatal("released id should miss")
	}
}
