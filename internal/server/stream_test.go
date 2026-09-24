package server

import (
	"context"
	"errors"
	"io"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// TestRunOrderedStream_Ordering verifies responses come back in input order.
func TestRunOrderedStream_Ordering(t *testing.T) {
	const n = 100
	jobs := make(chan int, 8)
	var mu sync.Mutex
	var got []int
	feed := make(chan int, n)
	for i := 1; i <= n; i++ {
		feed <- i
	}
	err := runOrderedStream(context.Background(), jobs,
		func() (uint64, string, int, error) {
			select {
			case j := <-feed:
				return uint64(j), "", j, nil
			case <-time.After(20 * time.Millisecond):
				return 0, "", 0, io.EOF
			}
		},
		func(j int) int { return j * 10 },
		func(r int) error {
			mu.Lock()
			got = append(got, r)
			mu.Unlock()
			return nil
		})
	if err != nil {
		t.Fatalf("stream: %v", err)
	}
	if len(got) != n {
		t.Fatalf("got %d results, want %d", len(got), n)
	}
	for i, v := range got {
		if v != (i+1)*10 {
			t.Fatalf("out of order at %d: %d", i, v)
		}
	}
}

// TestRunOrderedStream_HalfStreamFailure verifies a send failure stops the
// stream promptly (no further conversions) and unblocks the receiver.
func TestRunOrderedStream_HalfStreamFailure(t *testing.T) {
	sendErr := errors.New("client stopped reading")
	jobs := make(chan int, 4)
	var handled int64
	stop := make(chan struct{})
	err := runOrderedStream(context.Background(), jobs,
		func() (uint64, string, int, error) {
			select {
			case <-stop:
				return 0, "", 0, io.EOF
			default:
			}
			return 1, "", 1, nil // always another item
		},
		func(j int) int { atomic.AddInt64(&handled, 1); return j },
		func(r int) error {
			if atomic.LoadInt64(&handled) >= 3 {
				return sendErr
			}
			return nil
		})
	if !errors.Is(err, sendErr) {
		t.Fatalf("expected send error, got %v", err)
	}
	close(stop)
	// After the half-stream failure no further handle() calls may occur.
	time.Sleep(30 * time.Millisecond)
	if h := atomic.LoadInt64(&handled); h > 3 {
		t.Fatalf("conversion continued after failure: %d handled", h)
	}
}

// TestRunOrderedStream_Backpressure verifies buffering is bounded: when sends
// stall, at most cap(jobs)+in-flight items may be read ahead.
func TestRunOrderedStream_Backpressure(t *testing.T) {
	const capB = 4
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	jobs := make(chan int, capB)
	var received, processed int64
	release := make(chan struct{})
	go func() {
		err := runOrderedStream(ctx, jobs,
			func() (uint64, string, int, error) {
				v := atomic.AddInt64(&received, 1)
				return uint64(v), "", int(v), nil
			},
			func(j int) int { atomic.AddInt64(&processed, 1); return j },
			func(r int) error {
				<-release // stall all sends
				return nil
			})
		_ = err
	}()
	time.Sleep(50 * time.Millisecond)
	got := atomic.LoadInt64(&received)
	proc := atomic.LoadInt64(&processed)
	// At most cap(jobs) may be buffered, plus at most one item in the
	// instant between recv() returning and the bounded enqueue blocking.
	if got-proc > capB+1 {
		t.Fatalf("backpressure violated: %d received but only %d processed (bound %d)",
			got, proc, capB+1)
	}
	if proc != 1 {
		t.Fatalf("exactly one item should be in-flight, got %d", proc)
	}
	// While the send stays stalled, read-ahead must not keep growing.
	time.Sleep(50 * time.Millisecond)
	if got2 := atomic.LoadInt64(&received); got2 != got {
		t.Fatalf("receiver read ahead while stalled: %d -> %d", got, got2)
	}
	close(release)
}

// TestRunOrderedStream_CancelStops verifies cancellation stops conversion
// even while waiting for input.
func TestRunOrderedStream_CancelStops(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	jobs := make(chan int, 2)
	go func() {
		done <- runOrderedStream(ctx, jobs,
			func() (uint64, string, int, error) {
				time.Sleep(time.Hour) // no input ever
				return 0, "", 0, io.EOF
			},
			func(j int) int { t.Error("handler must not run after cancel"); return j },
			func(r int) error { return nil })
	}()
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("expected context.Canceled, got %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("cancellation did not stop the stream")
	}
}
