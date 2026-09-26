// Package testutil holds small helpers shared by the test suites:
// asynchronous assertions and goroutine/heap baselines for leak checks.
package testutil

import (
	"runtime"
	"testing"
	"time"
)

// Eventually polls cond until it returns true or the wait elapses. It reports
// a failure with msg if the condition never settles.
func Eventually(t *testing.T, wait time.Duration, cond func() bool, msg string) {
	t.Helper()
	deadline := time.Now().Add(wait)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
		runtime.Gosched()
	}
	if !cond() {
		t.Fatalf("condition never became true within %s: %s", wait, msg)
	}
}

// Baseline captures runtime state before a workload.
type Baseline struct {
	Goroutines int
	HeapAlloc  uint64
}

// Snapshot takes a runtime measurement after forcing GC.
func Snapshot() Baseline {
	runtime.GC()
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	return Baseline{Goroutines: runtime.NumGoroutine(), HeapAlloc: ms.HeapAlloc}
}

// GoroutineSettled polls until NumGoroutine drops to <= want (or the wait
// elapses). It yields and briefly sleeps so torn-down HTTP server goroutines
// have time to exit.
func GoroutineSettled(t *testing.T, want int, wait time.Duration) {
	t.Helper()
	Eventually(t, wait, func() bool { return runtime.NumGoroutine() <= want },
		"goroutines never settled")
	if runtime.NumGoroutine() > want {
		t.Fatalf("goroutine leak: want <= %d, got %d", want, runtime.NumGoroutine())
	}
}
