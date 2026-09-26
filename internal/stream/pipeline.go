// Package stream contains the bounded-buffer pipeline that sits between an
// upstream source and the HTTP response writer. A slow consumer fills the
// bounded buffer, which blocks the producer, which applies backpressure to
// the upstream — so memory per connection stays bounded no matter how fast
// the upstream produces.
package stream

import (
	"context"
	"errors"
	"io"
	"sync"
)

// Source produces the next item of a result stream. It returns io.EOF when
// the stream is exhausted.
type Source interface {
	Next(ctx context.Context) ([]byte, error)
}

// Limiter is a process-wide byte semaphore bounding the total memory held
// by in-flight buffers across all connections. Acquire blocks (applying
// backpressure) when the budget is exhausted.
type Limiter struct {
	max    int64
	mu     sync.Mutex
	cur    int64
	notify chan struct{}
}

func NewLimiter(maxBytes int64) *Limiter {
	return &Limiter{max: maxBytes, notify: make(chan struct{}, 1)}
}

// Acquire reserves n bytes, blocking until budget is free or ctx is done.
// It reports whether the reservation was made.
func (l *Limiter) Acquire(ctx context.Context, n int64) bool {
	for {
		l.mu.Lock()
		if l.cur+n <= l.max {
			l.cur += n
			l.mu.Unlock()
			return true
		}
		l.mu.Unlock()
		select {
		case <-l.notify:
		case <-ctx.Done():
			return false
		}
	}
}

// Release returns n bytes of budget and wakes one waiter.
func (l *Limiter) Release(n int64) {
	l.mu.Lock()
	l.cur -= n
	if l.cur < 0 {
		l.cur = 0
	}
	l.mu.Unlock()
	select {
	case l.notify <- struct{}{}:
	default:
	}
}

// Current reports the currently reserved bytes (observability gauge).
func (l *Limiter) Current() int64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.cur
}

// Max reports the configured byte budget.
func (l *Limiter) Max() int64 { return l.max }

// Result is one producer output: either an item or a terminal error.
type Result struct {
	Data []byte
	Err  error
}

// Produce reads items from src into a bounded channel of buf slots. Every
// buffered byte is reserved against lim first, so a stalled consumer turns
// into backpressure on src instead of unbounded memory growth. The channel
// is closed when src is exhausted, errors, or ctx is cancelled; the caller
// must keep draining (see Drain) so limiter reservations are released.
func Produce(ctx context.Context, src Source, buf int, lim *Limiter) <-chan Result {
	out := make(chan Result, buf)
	go func() {
		defer close(out)
		for {
			data, err := src.Next(ctx)
			if errors.Is(err, io.EOF) {
				return
			}
			if err != nil {
				if ctx.Err() != nil {
					return
				}
				select {
				case out <- Result{Err: err}:
				case <-ctx.Done():
				}
				return
			}
			if !lim.Acquire(ctx, int64(len(data))) {
				return
			}
			select {
			case out <- Result{Data: data}:
			case <-ctx.Done():
				lim.Release(int64(len(data)))
				return
			}
		}
	}()
	return out
}

// Drain consumes and discards the rest of ch, releasing limiter
// reservations. Call it when abandoning a stream early (client disconnect,
// terminal error) so reserved memory is returned promptly.
func Drain(ch <-chan Result, lim *Limiter) {
	for r := range ch {
		if r.Err == nil {
			lim.Release(int64(len(r.Data)))
		}
	}
}
