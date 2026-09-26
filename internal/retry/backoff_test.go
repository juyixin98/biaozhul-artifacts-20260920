package retry

import (
	"math/rand"
	"testing"
	"time"
)

func TestBudgetTryAcquire(t *testing.T) {
	b := NewBudget(2)
	if !b.TryAcquire() || !b.TryAcquire() {
		t.Fatal("first two acquisitions should succeed")
	}
	if b.TryAcquire() {
		t.Fatal("third acquisition should fail (exhausted)")
	}
	if got := b.Remaining(); got != 0 {
		t.Fatalf("remaining = %d, want 0", got)
	}
}

func TestBackoffDeterministicWithoutRand(t *testing.T) {
	b := Backoff{Base: 100 * time.Millisecond, Max: 500 * time.Millisecond, Multiplier: 2}
	want := []time.Duration{100, 200, 400, 500, 500}
	for i, w := range want {
		if got := b.Delay(i + 1); got != w*time.Millisecond {
			t.Fatalf("Delay(%d) = %v, want %v", i+1, got, w*time.Millisecond)
		}
	}
}

func TestBackoffJitterBoundedWithSeededRand(t *testing.T) {
	b := Backoff{Base: 100 * time.Millisecond, Max: 10 * time.Second, Multiplier: 2, Rand: rand.New(rand.NewSource(7))}
	for attempt := 1; attempt <= 5; attempt++ {
		base := Backoff{Base: 100 * time.Millisecond, Max: 10 * time.Second, Multiplier: 2}.Delay(attempt)
		got := b.Delay(attempt)
		if got < base/2 || got > base {
			t.Fatalf("Delay(%d) = %v outside [%v, %v]", attempt, got, base/2, base)
		}
	}
}

func TestParseRetryAfter(t *testing.T) {
	now := time.Now()
	if d, ok := parseRetryAfter("3", now); !ok || d != 3*time.Second {
		t.Fatalf("delta-seconds: %v %v", d, ok)
	}
	if d, ok := parseRetryAfter("0", now); !ok || d != 0 {
		t.Fatalf("zero delta: %v %v", d, ok)
	}
	if _, ok := parseRetryAfter("", now); ok {
		t.Fatal("empty header should not parse")
	}
	if _, ok := parseRetryAfter("garbage", now); ok {
		t.Fatal("garbage should not parse")
	}
	if d, ok := parseRetryAfter(now.Add(2*time.Second).UTC().Format(httpTimeFormat), now); !ok || d <= 0 {
		t.Fatalf("http-date: %v %v", d, ok)
	}
}

const httpTimeFormat = "Mon, 02 Jan 2006 15:04:05 GMT"
