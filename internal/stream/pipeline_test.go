package stream

import (
	"context"
	"io"
	"sync/atomic"
	"testing"
	"time"
)

type sliceSource struct {
	items [][]byte
	i     int
	// produced counts items handed out, for backpressure assertions.
	produced *atomic.Int64
}

func (s *sliceSource) Next(context.Context) ([]byte, error) {
	if s.i >= len(s.items) {
		return nil, io.EOF
	}
	b := s.items[s.i]
	s.i++
	if s.produced != nil {
		s.produced.Add(1)
	}
	return b, nil
}

func items(n, size int) [][]byte {
	out := make([][]byte, n)
	for i := range out {
		out[i] = make([]byte, size)
	}
	return out
}

func TestProduceBoundedBufferAppliesBackpressure(t *testing.T) {
	var produced atomic.Int64
	src := &sliceSource{items: items(100, 16), produced: &produced}
	lim := NewLimiter(1 << 20)
	ch := Produce(context.Background(), src, 4, lim)

	// Give the producer time to run ahead of the (absent) consumer.
	time.Sleep(50 * time.Millisecond)
	if got := produced.Load(); got > 5 { // buffer 4 + 1 in flight
		t.Fatalf("producer ran ahead: produced=%d with buffer=4 and no consumer", got)
	}
	// Buffered reservations must be visible and bounded: buffer slots plus
	// at most one item acquired while blocked on the full channel.
	if got := lim.Current(); got > 5*16 {
		t.Fatalf("reserved bytes %d exceed buffer bound", got)
	}
	n := 0
	for r := range ch { // consumer releases each reservation after use
		lim.Release(int64(len(r.Data)))
		n++
	}
	if n != 100 {
		t.Fatalf("got %d items, want 100", n)
	}
	if got := lim.Current(); got != 0 {
		t.Fatalf("limiter leaked: %d bytes still reserved", got)
	}
}

func TestProduceStopsOnCancel(t *testing.T) {
	src := &sliceSource{items: items(1000, 64)}
	lim := NewLimiter(1 << 20)
	ctx, cancel := context.WithCancel(context.Background())
	ch := Produce(ctx, src, 2, lim)
	r := <-ch
	lim.Release(int64(len(r.Data))) // consumer side releases what it took
	cancel()
	Drain(ch, lim)
	if got := lim.Current(); got != 0 {
		t.Fatalf("limiter leaked after cancel: %d", got)
	}
}

func TestLimiterBlocksAtBudget(t *testing.T) {
	lim := NewLimiter(100)
	if !lim.Acquire(context.Background(), 60) {
		t.Fatal("first acquire failed")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if lim.Acquire(ctx, 60) {
		t.Fatal("acquire beyond budget should block until ctx done")
	}
	lim.Release(60)
	if !lim.Acquire(context.Background(), 100) {
		t.Fatal("acquire after release failed")
	}
}
