package netlink

import "testing"

func TestDeterministicPlan(t *testing.T) {
	l := Link{BaseDelay: 2, Loss: 0.3, Duplicate: 0.3, Jitter: 0.5}
	p1 := New(2, l, nil, 99).Send("A1", 0, 1, 0)
	p2 := New(2, l, nil, 99).Send("A1", 0, 1, 0)
	if len(p1.Arrivals) != len(p2.Arrivals) || p1.DropReason != p2.DropReason {
		t.Fatalf("same seed produced different plans: %+v vs %+v", p1, p2)
	}
	for i := range p1.Arrivals {
		if p1.Arrivals[i] != p2.Arrivals[i] {
			t.Fatalf("arrival %d differs: %f vs %f", i, p1.Arrivals[i], p2.Arrivals[i])
		}
	}
}

func TestForcedDrop(t *testing.T) {
	nw := New(2, Link{BaseDelay: 1}, nil, 1)
	nw.ForceDrop("A1", -1)
	if p := nw.Send("A1", 0, 1, 0); p.DropReason != "forced" || len(p.Arrivals) != 0 {
		t.Fatalf("want forced drop, got %+v", p)
	}
	if p := nw.Send("A2", 0, 1, 0); p.DropReason != "" {
		t.Fatalf("A2 should pass, got %+v", p)
	}
}

func TestHoldAddsCopies(t *testing.T) {
	nw := New(3, Link{BaseDelay: 1}, nil, 1)
	nw.ForceHold("A1", 2, 9.0)
	p := nw.Send("A1", 0, 2, 0)
	if len(p.Arrivals) != 2 {
		t.Fatalf("want normal + held copy, got %d arrivals", len(p.Arrivals))
	}
	// The held copy keeps its absolute-relative time.
	found := false
	for _, at := range p.Arrivals {
		if at == 9.0 {
			found = true
		}
	}
	if !found {
		t.Fatalf("held copy at t=9 missing: %v", p.Arrivals)
	}
}

func TestDelayReplacesNormalAndDropWins(t *testing.T) {
	nw := New(2, Link{BaseDelay: 1}, nil, 1)
	nw.ForceDelay("A1", 1, 5.0, 7.0)
	p := nw.Send("A1", 0, 1, 0)
	if len(p.Arrivals) != 2 {
		t.Fatalf("want exactly 2 scripted copies, got %d", len(p.Arrivals))
	}
	// drop overrides delay
	nw.ForceDrop("A1", 1)
	if p := nw.Send("A1", 0, 1, 0); p.DropReason != "forced" {
		t.Fatalf("drop must beat delay, got %+v", p)
	}
}
