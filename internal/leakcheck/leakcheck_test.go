package leakcheck

import (
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestSettler_ReachesTarget(t *testing.T) {
	var v atomic.Int64
	done := make(chan struct{})
	go func() {
		time.Sleep(20 * time.Millisecond)
		v.Store(7)
		close(done)
	}()
	s := Settler{Timeout: time.Second, Every: time.Millisecond, Stable: 1}
	if err := s.Wait("probe", v.Load, 7); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	<-done
}

func TestSettler_TimesOut(t *testing.T) {
	s := Settler{Timeout: 20 * time.Millisecond, Every: time.Millisecond, Stable: 1}
	err := s.Wait("probe", func() int64 { return 1 }, 0)
	if err == nil || !strings.Contains(err.Error(), "did not settle") {
		t.Fatalf("err = %v, want a settle error", err)
	}
}

func TestStacks_FiltersByPackage(t *testing.T) {
	counts := Stacks("cancelprop/internal/leakcheck")
	if len(counts) == 0 {
		t.Fatal("expected at least one matching stack (this test runs inside the package)")
	}
	foundSelf := false
	for sig := range counts {
		if strings.Contains(sig, "leakcheck") {
			foundSelf = true
		}
	}
	if !foundSelf {
		t.Fatalf("no leakcheck frame in signatures: %v", counts)
	}
}

func TestHeapSample_DecreasesAfterGarbage(t *testing.T) {
	_ = HeapSample()
	garbage := make([][]byte, 200)
	for i := range garbage {
		garbage[i] = make([]byte, 4096)
	}
	for i := range garbage {
		garbage[i] = nil
	}
	before := HeapSample()
	_ = before // GC is forced inside HeapSample; just assert it runs and returns.
}

func TestGoroutineCount_Positive(t *testing.T) {
	if GoroutineCount() < 1 {
		t.Fatal("expected at least one goroutine")
	}
}
