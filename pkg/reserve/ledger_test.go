package reserve

import (
	"errors"
	"testing"

	quotav1alpha1 "github.com/biaozhul/quota-reserver/api/v1alpha1"
)

func spec(cpu, mem int64) quotav1alpha1.QuotaPoolSpec {
	return quotav1alpha1.QuotaPoolSpec{CPUMilli: cpu, MemoryBytes: mem}
}

func TestReserveAndRecompute(t *testing.T) {
	st := &quotav1alpha1.QuotaPoolStatus{}
	already, err := Reserve(st, spec(1000, 1<<30), "uid-1", "a", 400, 1<<28)
	if err != nil || already {
		t.Fatalf("first reserve: already=%v err=%v", already, err)
	}
	if st.UsedCPUMilli != 400 || st.UsedMemoryBytes != 1<<28 {
		t.Fatalf("used = %dm/%dB, want 400m/%dB", st.UsedCPUMilli, st.UsedMemoryBytes, 1<<28)
	}
	if len(st.Allocations) != 1 || st.Allocations[0].RequestUID != "uid-1" {
		t.Fatalf("ledger = %+v", st.Allocations)
	}
}

func TestReserveIsIdempotentPerUID(t *testing.T) {
	st := &quotav1alpha1.QuotaPoolStatus{}
	if _, err := Reserve(st, spec(1000, 1<<30), "uid-1", "a", 400, 1<<28); err != nil {
		t.Fatal(err)
	}
	already, err := Reserve(st, spec(1000, 1<<30), "uid-1", "a", 400, 1<<28)
	if err != nil {
		t.Fatal(err)
	}
	if !already {
		t.Fatal("second reserve of same UID must report already=true")
	}
	if st.UsedCPUMilli != 400 || len(st.Allocations) != 1 {
		t.Fatalf("double count detected: used=%dm allocs=%+v", st.UsedCPUMilli, st.Allocations)
	}
}

func TestReserveExceedsCapacity(t *testing.T) {
	st := &quotav1alpha1.QuotaPoolStatus{}
	if _, err := Reserve(st, spec(1000, 1<<30), "uid-1", "a", 900, 1<<28); err != nil {
		t.Fatal(err)
	}
	// CPU would exceed.
	if _, err := Reserve(st, spec(1000, 1<<30), "uid-2", "b", 200, 1); !errors.Is(err, ErrExceedsCapacity) {
		t.Fatalf("want ErrExceedsCapacity, got %v", err)
	}
	// Memory would exceed.
	if _, err := Reserve(st, spec(1000, 1<<30), "uid-3", "c", 1, 1<<30); !errors.Is(err, ErrExceedsCapacity) {
		t.Fatalf("want ErrExceedsCapacity, got %v", err)
	}
	// Exactly at capacity is allowed.
	if _, err := Reserve(st, spec(1000, 1<<30), "uid-4", "d", 100, (1<<30)-(1<<28)); err != nil {
		t.Fatalf("exact-fit reserve must succeed: %v", err)
	}
	if st.UsedCPUMilli != 1000 || st.UsedMemoryBytes != 1<<30 {
		t.Fatalf("used = %dm/%dB, want 1000m/%dB", st.UsedCPUMilli, st.UsedMemoryBytes, 1<<30)
	}
}

func TestRelease(t *testing.T) {
	st := &quotav1alpha1.QuotaPoolStatus{}
	_, _ = Reserve(st, spec(1000, 1<<30), "uid-1", "a", 400, 1<<28)
	_, _ = Reserve(st, spec(1000, 1<<30), "uid-2", "b", 500, 1<<28)

	if !Release(st, "uid-1") {
		t.Fatal("release of existing uid must return true")
	}
	if st.UsedCPUMilli != 500 || len(st.Allocations) != 1 {
		t.Fatalf("after release: used=%dm allocs=%+v", st.UsedCPUMilli, st.Allocations)
	}
	// Releasing again is a no-op.
	if Release(st, "uid-1") {
		t.Fatal("second release must return false")
	}
	if Release(st, "uid-unknown") {
		t.Fatal("release of unknown uid must return false")
	}
	if st.UsedCPUMilli != 500 {
		t.Fatalf("used changed after no-op release: %dm", st.UsedCPUMilli)
	}
}

func TestSum(t *testing.T) {
	cpu, mem := Sum([]quotav1alpha1.Allocation{
		{RequestUID: "a", CPUMilli: 100, MemoryBytes: 5},
		{RequestUID: "b", CPUMilli: 250, MemoryBytes: 7},
	})
	if cpu != 350 || mem != 12 {
		t.Fatalf("sum = %dm/%dB, want 350m/12B", cpu, mem)
	}
}
