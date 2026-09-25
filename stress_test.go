package batchagg

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// TestStress_ConcurrentKeysAndCancellations hammers the scheduler with many
// keys, goroutines and occasional cancellations against the wall clock, and
// asserts every non-canceled item gets exactly one independent result.
func TestStress_ConcurrentKeysAndCancellations(t *testing.T) {
	exec := &fakeExec{}
	rec := NewEventRecorder()
	s := New(Config{
		MaxItems: 7,
		MaxBytes: 2048,
		MaxWait:  5 * time.Millisecond,
		Clock:    NewSystemClock(),
		Sink:     rec,
	}, exec)

	const workers = 16
	const perWorker = 100
	var wg sync.WaitGroup
	var success, canceled, rejected atomic.Int64

	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; i < perWorker; i++ {
				key := fmt.Sprintf("key-%d", (w*7+i)%5)
				ctx := context.Background()
				cancelFn := func() {}
				// Roughly 10% of callers cancel immediately and race the
				// cancellation with dispatch. Either outcome is acceptable,
				// but it must never corrupt another item.
				var cancel context.CancelFunc
				if (w*perWorker+i)%10 == 0 {
					ctx, cancel = context.WithCancel(context.Background())
					cancelFn = cancel
				}
				id := fmt.Sprintf("w%d-i%d", w, i)
				fut, err := s.Submit(ctx, Item{
					ID:      id,
					Key:     key,
					Payload: []byte(fmt.Sprintf("payload-%d", i%50)),
				})
				if err != nil {
					if errors.Is(err, context.Canceled) {
						canceled.Add(1)
						cancelFn()
						continue
					}
					t.Errorf("submit %s: %v", id, err)
					continue
				}
				cancelFn() // no-op unless a cancelable ctx was built

				res, err := fut.Get(context.Background())
				switch {
				case err != nil:
					if errors.Is(err, context.Canceled) {
						canceled.Add(1)
					} else {
						t.Errorf("get %s: %v", id, err)
					}
				case res == nil:
					t.Errorf("get %s: nil result, nil err", id)
				default:
					success.Add(1)
				}
			}
		}(w)
	}
	wg.Wait()

	if err := s.Close(); err != nil {
		t.Fatal(err)
	}

	total := success.Load() + canceled.Load() + rejected.Load()
	if total != workers*perWorker {
		t.Fatalf("accounted=%d (ok=%d canceled=%d rejected=%d), want %d",
			total, success.Load(), canceled.Load(), rejected.Load(), workers*perWorker)
	}

	// Every successful result event must be paired 1:1 with a successful
	// future; batches may contain individual failures but here none are
	// injected, so all result events should be successful.
	for _, ev := range rec.OfType(EventItemResult) {
		if !ev.Success {
			t.Fatalf("unexpected failed event: %+v", ev)
		}
	}
	if got := len(rec.OfType(EventItemResult)); int64(got) != success.Load() {
		t.Fatalf("result events=%d successful futures=%d", got, success.Load())
	}
	t.Logf("stress: %d succeeded, %d canceled, %d batches executed",
		success.Load(), canceled.Load(), exec.callCount())
}
