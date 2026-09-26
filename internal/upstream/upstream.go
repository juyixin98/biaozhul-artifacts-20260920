// Package upstream implements the in-process fake result source. It stands
// in for a real dependency (database, search backend, ...) and supports
// fault injection: configurable per-item delay and a deterministic failure
// after N items.
package upstream

import (
	"context"
	"errors"
	"fmt"
	"io"
	"time"

	"streambp/internal/clock"
)

// ErrInjected is returned once the configured FailAfter threshold is hit.
var ErrInjected = errors.New("upstream: injected failure")

// Config controls the fake upstream.
type Config struct {
	// ItemCount is the total number of items the stream would produce.
	ItemCount int
	// ItemSize is the byte size of each item payload.
	ItemSize int
	// Delay is the per-item production latency (uses the injected clock).
	Delay time.Duration
	// FailAfter makes Next return ErrInjected once this many items were
	// produced. A negative value disables fault injection.
	FailAfter int
}

// Fake is an in-process upstream source. Not safe for concurrent use; each
// stream gets its own instance.
type Fake struct {
	cfg Config
	clk clock.Clock
	seq int
}

func NewFake(cfg Config, clk clock.Clock) *Fake { return &Fake{cfg: cfg, clk: clk} }

// Next returns the next item, io.EOF when the stream is exhausted, or
// ErrInjected when the fault-injection threshold is reached.
func (f *Fake) Next(ctx context.Context) ([]byte, error) {
	if f.seq >= f.cfg.ItemCount {
		return nil, io.EOF
	}
	if f.cfg.FailAfter >= 0 && f.seq >= f.cfg.FailAfter {
		return nil, ErrInjected
	}
	if err := f.clk.Sleep(ctx, f.cfg.Delay); err != nil {
		return nil, err
	}
	item := makeItem(f.seq, f.cfg.ItemSize)
	f.seq++
	return item, nil
}

// makeItem builds a deterministic, human-inspectable payload of exactly
// size bytes, e.g. "item-000042:aaaaaaa...".
func makeItem(seq, size int) []byte {
	prefix := fmt.Sprintf("item-%06d:", seq)
	if size < len(prefix) {
		size = len(prefix)
	}
	b := make([]byte, size)
	copy(b, prefix)
	for i := len(prefix); i < size; i++ {
		b[i] = 'a' + byte(seq%26)
	}
	return b
}
