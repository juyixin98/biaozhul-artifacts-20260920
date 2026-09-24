package ledger

import (
	"sync"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	quota "resourcequota-reservation/api/v1alpha1"
)

func timeParseDuration(s string) (time.Duration, error) { return time.ParseDuration(s) }

func TestParseCPU(t *testing.T) {
	cases := []struct {
		in   string
		want int64
	}{
		{"1", 1000},
		{"100m", 100},
		{"0.5", 500},
		{"1500m", 1500},
		{"64", 64000},
	}
	for _, c := range cases {
		got, err := ParseCPU(c.in)
		if err != nil {
			t.Fatalf("ParseCPU(%q): %v", c.in, err)
		}
		if got != c.want {
			t.Errorf("ParseCPU(%q)=%d want %d", c.in, got, c.want)
		}
	}
	if _, err := ParseCPU("not-a-quantity"); err == nil {
		t.Error("expected parse error")
	}
	if _, err := ParseCPU("-1"); err == nil {
		t.Error("expected negative error")
	}
}

func TestParseMemory(t *testing.T) {
	cases := []struct {
		in   string
		want int64
	}{
		{"128Mi", 128 * 1024 * 1024},
		{"1Gi", 1024 * 1024 * 1024},
		{"1024", 1024},
	}
	for _, c := range cases {
		got, err := ParseMemory(c.in)
		if err != nil {
			t.Fatalf("ParseMemory(%q): %v", c.in, err)
		}
		if got != c.want {
			t.Errorf("ParseMemory(%q)=%d want %d", c.in, got, c.want)
		}
	}
}

func TestValidateClaimSpecBounds(t *testing.T) {
	base := func(cpu, mem, ttl string) quota.ResourceClaimSpec {
		d, _ := timeParseDuration(ttl)
		return quota.ResourceClaimSpec{CPU: cpu, Memory: mem, TTL: metav1.Duration{Duration: d}}
	}
	if _, _, err := ValidateClaimSpec(base("1m", "1Mi", "1s")); err != nil {
		t.Errorf("minimum valid spec rejected: %v", err)
	}
	if _, _, err := ValidateClaimSpec(base("64001m", "1Mi", "1s")); err == nil {
		t.Error("cpu above cap accepted")
	}
	if _, _, err := ValidateClaimSpec(base("1m", "257Gi", "1s")); err == nil {
		t.Error("memory above cap accepted")
	}
	if _, _, err := ValidateClaimSpec(base("1m", "1Mi", "25h")); err == nil {
		t.Error("ttl above cap accepted")
	}
	if _, _, err := ValidateClaimSpec(base("0m", "1Mi", "1s")); err == nil {
		t.Error("zero cpu accepted")
	}
}

func newPool(cpuCap, memCap string) *quota.ReservationPool {
	return &quota.ReservationPool{
		ObjectMeta: metav1.ObjectMeta{Name: quota.PoolName},
		Spec:       quota.PoolSpec{CPUCapacity: cpuCap, MemoryCapacity: memCap},
	}
}

func TestEnsureDebitedIdempotentAndCapacity(t *testing.T) {
	pool := newPool("1000m", "1Gi")
	uid := types.UID("uid-A")

	changed, err := EnsureDebited(pool, uid, "claim-a", 600, 512*1024*1024)
	if err != nil || !changed {
		t.Fatalf("first debit: changed=%v err=%v", changed, err)
	}
	cpu, mem := Used(pool)
	if cpu != 600 || mem != 512*1024*1024 {
		t.Fatalf("totals after debit = %dm,%d", cpu, mem)
	}

	// Same request again (simulating repeated reconcile / restart): no double charge.
	changed, err = EnsureDebited(pool, uid, "claim-a", 600, 512*1024*1024)
	if err != nil || changed {
		t.Fatalf("repeat debit should be no-op: changed=%v err=%v", changed, err)
	}
	if len(pool.Status.Claims) != 1 {
		t.Fatalf("claims map should have 1 entry, got %d", len(pool.Status.Claims))
	}

	// Different claim that would exceed cpu capacity.
	_, err = EnsureDebited(pool, types.UID("uid-B"), "claim-b", 500, 1)
	if err == nil {
		t.Fatal("expected ErrInsufficientCapacity")
	}
	if _, ok := err.(*ErrInsufficientCapacity); !ok {
		t.Fatalf("wrong error type: %T", err)
	}
	if _, ok := pool.Status.Claims["uid-B"]; ok {
		t.Fatal("rejected debit must not be recorded")
	}

	// Smaller claim fitting in remaining memory but not cpu also rejected;
	// one fitting both dimensions succeeds.
	if _, err := EnsureDebited(pool, types.UID("uid-C"), "claim-c", 400, 512*1024*1024); err != nil {
		t.Fatalf("claim within remaining capacity rejected: %v", err)
	}
	cpu, _ = Used(pool)
	if cpu != 1000 {
		t.Fatalf("cpu used = %d want 1000", cpu)
	}
}

func TestCreditIdempotent(t *testing.T) {
	pool := newPool("1000m", "1Gi")
	uid := types.UID("uid-A")
	if _, err := EnsureDebited(pool, uid, "claim-a", 300, 256*1024*1024); err != nil {
		t.Fatal(err)
	}
	if !Credit(pool, uid) {
		t.Fatal("first credit should report change")
	}
	if Credit(pool, uid) {
		t.Fatal("second credit must be no-op")
	}
	cpu, mem := Used(pool)
	if cpu != 0 || mem != 0 {
		t.Fatalf("totals after credit = %dm,%d want 0", cpu, mem)
	}
	if len(pool.Status.Claims) != 0 {
		t.Fatalf("credited entry should be deleted, got %d", len(pool.Status.Claims))
	}
}

func TestConcurrentDebitSimulation(t *testing.T) {
	// Simulates the optimistic-concurrency protocol at the logic level:
	// N goroutines serialize through a mutex standing in for the API server's
	// resourceVersion CAS; the pool must never be overcommitted.
	pool := newPool("2000m", "2Gi")
	const n = 8
	var wg sync.WaitGroup
	var mu sync.Mutex
	rejected := make(chan string, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			uid := types.UID(rune('A' + i))
			mu.Lock() // API server serializes status updates with matching RV
			defer mu.Unlock()
			if _, err := EnsureDebited(pool, uid, "c", 500, 400*1024*1024); err != nil {
				rejected <- string(uid)
			}
		}(i)
	}
	wg.Wait()
	close(rejected)
	var nRej int
	for range rejected {
		nRej++
	}
	cpu, mem := Used(pool)
	if cpu > 2000 || mem > 2*1024*1024*1024 {
		t.Fatalf("overcommit: cpu=%d mem=%d", cpu, mem)
	}
	if nRej != 4 {
		t.Fatalf("expected 4 rejections (8x500 into 2000), got %d", nRej)
	}
}
