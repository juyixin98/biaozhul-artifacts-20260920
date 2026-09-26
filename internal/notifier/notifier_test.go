package notifier

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"etagrace/internal/clock"
)

func TestFaultyFailsExactlyNThenRecovers(t *testing.T) {
	clk := clock.NewFake(time.Now())
	log := NewAuditLog()
	f := NewFaulty(log, clk)
	f.ArmFailures(2)

	for i := 0; i < 2; i++ {
		if err := f.Notify(context.Background(), Event{Type: "update"}); !errors.Is(err, ErrInjected) {
			t.Fatalf("call %d: want ErrInjected, got %v", i, err)
		}
	}
	if err := f.Notify(context.Background(), Event{Type: "update"}); err != nil {
		t.Fatalf("third call should succeed: %v", err)
	}
	if log.Len() != 1 {
		t.Fatalf("exactly one event logged, got %d", log.Len())
	}
	if f.Remaining() != 0 {
		t.Fatalf("remaining=%d", f.Remaining())
	}
}

func TestFaultyNegativeFailsForeverUntilDisarm(t *testing.T) {
	clk := clock.NewFake(time.Now())
	log := NewAuditLog()
	f := NewFaulty(log, clk)
	f.ArmFailures(-1)

	for i := 0; i < 5; i++ {
		if err := f.Notify(context.Background(), Event{}); !errors.Is(err, ErrInjected) {
			t.Fatalf("call %d should keep failing", i)
		}
	}
	f.Disarm()
	if err := f.Notify(context.Background(), Event{}); err != nil {
		t.Fatalf("after disarm: %v", err)
	}
	if log.Len() != 1 {
		t.Fatalf("log len=%d", log.Len())
	}
}

// The exact failure count must hold when many goroutines race Notify: exactly
// n failures regardless of scheduling.
func TestFaultyFailureCountUnderConcurrency(t *testing.T) {
	clk := clock.NewFake(time.Now())
	log := NewAuditLog()
	f := NewFaulty(log, clk)
	const armed = 10
	f.ArmFailures(armed)

	var wg sync.WaitGroup
	var mu sync.Mutex
	failed, succeeded := 0, 0
	start := make(chan struct{})
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			err := f.Notify(context.Background(), Event{})
			mu.Lock()
			if err != nil {
				failed++
			} else {
				succeeded++
			}
			mu.Unlock()
		}()
	}
	close(start)
	wg.Wait()

	if failed != armed || succeeded != 50-armed {
		t.Fatalf("want %d failures and %d successes, got %d/%d", armed, 50-armed, failed, succeeded)
	}
	if log.Len() != 50-armed {
		t.Fatalf("audit log len=%d", log.Len())
	}
}

func TestReliableNotifierAndAuditSnapshot(t *testing.T) {
	log := NewAuditLog()
	r := NewReliable(log)
	e := Event{Type: "create", Key: "k", Version: 1}
	if err := r.Notify(context.Background(), e); err != nil {
		t.Fatal(err)
	}
	events := log.Events()
	if len(events) != 1 || events[0].Key != "k" {
		t.Fatalf("events=%+v", events)
	}
	// Mutating the returned slice must not affect the log.
	events[0].Key = "tampered"
	if log.Events()[0].Key != "k" {
		t.Fatal("Events() must return a defensive copy")
	}
}
