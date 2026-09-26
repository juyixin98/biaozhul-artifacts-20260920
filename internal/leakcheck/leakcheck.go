// Package leakcheck provides small, deterministic helpers for asserting
// that goroutines, connections and memory do not grow without bound across
// repeated requests. It is used by the automated tests.
package leakcheck

import (
	"bytes"
	"fmt"
	"runtime"
	"strings"
	"time"
)

// GoroutineCount is the current number of live goroutines.
func GoroutineCount() int { return runtime.NumGoroutine() }

// Stacks returns a map of "top user function" -> count for goroutines whose
// stack contains the given substring (e.g. "cancelprop"). It is used to
// attribute leaked goroutines to this project rather than the runtime.
func Stacks(contains string) map[string]int {
	buf := make([]byte, 1<<20)
	n := runtime.Stack(buf, true)
	blocks := bytes.Split(buf[:n], []byte("\n\n"))
	counts := map[string]int{}
	for _, b := range blocks {
		s := string(b)
		if contains != "" && !strings.Contains(s, contains) {
			continue
		}
		counts[stackSignature(s)]++
	}
	return counts
}

// stackSignature extracts a stable, short identifier from a goroutine block.
func stackSignature(block string) string {
	lines := strings.Split(block, "\n")
	for _, ln := range lines[1:] {
		ln = strings.TrimSpace(ln)
		if ln == "" || strings.HasPrefix(ln, "/") || strings.HasPrefix(ln, "created by") {
			continue
		}
		// Drop argument offsets: pkg.Func(...) -> pkg.Func
		if i := strings.Index(ln, "("); i > 0 {
			return ln[:i]
		}
		return ln
	}
	if len(lines) > 0 {
		return lines[0]
	}
	return "unknown"
}

// Settler repeatedly evaluates value until it equals target and stays equal
// for stable consecutive samples, or until timeout elapses.
type Settler struct {
	Timeout time.Duration
	Every   time.Duration
	Stable  int // consecutive equal samples required
}

// DefaultSettler tolerates normal scheduling jitter while failing fast
// enough to keep tests quick.
func DefaultSettler() Settler {
	return Settler{Timeout: 2 * time.Second, Every: 10 * time.Millisecond, Stable: 20}
}

// Wait runs value and waits for it to settle at target.
func (s Settler) Wait(name string, value func() int64, target int64) error {
	deadline := time.Now().Add(s.Timeout)
	same := 0
	var last int64
	for time.Now().Before(deadline) {
		last = value()
		if last == target {
			same++
			if same >= s.Stable {
				return nil
			}
		} else {
			same = 0
		}
		time.Sleep(s.Every)
	}
	return fmt.Errorf("leakcheck: %s did not settle at %d within %s (last=%d)", name, target, s.Timeout, last)
}

// HeapSample forces a GC and returns live heap bytes.
func HeapSample() uint64 {
	runtime.GC()
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	return ms.HeapAlloc
}
