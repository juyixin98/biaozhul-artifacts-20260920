package resguard

import "testing"

func TestRegistry_AcquireReleaseBalances(t *testing.T) {
	r := NewRegistry()
	if r.Active() != 0 {
		t.Fatal("new registry not empty")
	}
	release := r.Acquire(1, "lock")
	if r.Active() != 1 {
		t.Fatalf("active = %d, want 1", r.Active())
	}
	if !release() {
		t.Fatal("first release must return true")
	}
	if r.Active() != 0 {
		t.Fatalf("active after release = %d, want 0", r.Active())
	}
	acq, rel := r.Totals()
	if acq != 1 || rel != 1 {
		t.Fatalf("totals %d/%d, want 1/1", acq, rel)
	}
}

func TestRegistry_DoubleReleaseIgnored(t *testing.T) {
	r := NewRegistry()
	release := r.Acquire(1, "lock")
	_ = release()
	if release() {
		t.Fatal("double release must return false and not decrement twice")
	}
	if r.Active() != 0 {
		t.Fatalf("active = %d, want 0", r.Active())
	}
	if _, rel := r.Totals(); rel != 1 {
		t.Fatalf("released total = %d, want 1", rel)
	}
}

func TestRegistry_DistinctResourcesAndReacquire(t *testing.T) {
	r := NewRegistry()
	rel1 := r.Acquire(1, "a")
	rel2 := r.Acquire(2, "b")
	if r.Active() != 2 {
		t.Fatalf("active = %d, want 2", r.Active())
	}
	_ = rel1()
	// Same request can re-acquire the same name after release.
	rel3 := r.Acquire(1, "a")
	if r.Active() != 2 {
		t.Fatalf("active after reacquire = %d, want 2", r.Active())
	}
	_ = rel2()
	_ = rel3()
	if r.Active() != 0 {
		t.Fatal("not fully released")
	}
}

func TestRegistry_SameNameHeldByDifferentRequests(t *testing.T) {
	r := NewRegistry()
	rel1 := r.Acquire(100, "shared-lock")
	rel2 := r.Acquire(200, "shared-lock") // different request: allowed
	if r.Active() != 2 {
		t.Fatalf("active = %d, want 2 (per-request namespacing)", r.Active())
	}
	_ = rel1()
	_ = rel2()
	if r.Active() != 0 {
		t.Fatal("not drained")
	}
}

func TestRegistry_DoubleAcquireWithinRequestPanics(t *testing.T) {
	r := NewRegistry()
	_ = r.Acquire(7, "lock")
	defer func() {
		if rec := recover(); rec == nil {
			t.Fatal("expected panic on double acquire within one request")
		}
	}()
	r.Acquire(7, "lock") // same request, still held -> panic
}
