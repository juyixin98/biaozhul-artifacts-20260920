// Package upstream is an in-process fake of an external result source.
// It replaces any production dependency: tests and demos configure item
// sizes, per-item delays and injected failures through Spec. Production
// is pull-based: items are only generated when the consumer's channel
// has room, so a blocked consumer applies backpressure to the source.
package upstream

import (
	"context"
	"errors"
	"fmt"
	"time"

	"streamback/internal/clock"
)

// Item is one unit of result data.
type Item struct {
	Seq     int
	Payload []byte
}

// Spec describes a fake upstream run, including fault injection.
type Spec struct {
	// Count is the total number of items the run would produce.
	Count int
	// ItemBytes is the payload size of each item.
	ItemBytes int
	// ItemDelay is the simulated production latency per item.
	ItemDelay int64 // milliseconds
	// FailAt, when > 0, makes the run fail instead of producing the
	// item with that 1-based sequence number.
	FailAt int
	// FailMsg is the error message injected at FailAt.
	FailMsg string
}

// Validate checks the spec for impossible values.
func (s Spec) Validate() error {
	if s.Count < 0 {
		return errors.New("count must be >= 0")
	}
	if s.ItemBytes < 0 {
		return errors.New("itemBytes must be >= 0")
	}
	if s.ItemDelay < 0 {
		return errors.New("itemDelayMs must be >= 0")
	}
	if s.FailAt < 0 {
		return errors.New("failAt must be >= 0")
	}
	return nil
}

// Service is the fake upstream. It is safe for concurrent use.
type Service struct {
	clk clock.Clock
}

// New builds a Service using clk for all delays.
func New(clk clock.Clock) *Service {
	return &Service{clk: clk}
}

// Stream starts producing items per spec. It returns an item channel and
// an error channel; both are closed when the run ends. At most one error
// is delivered, after which the run terminates. Cancelling ctx stops
// production promptly; the channels are then closed as well.
func (s *Service) Stream(ctx context.Context, spec Spec) (<-chan Item, <-chan error) {
	items := make(chan Item)
	errs := make(chan error, 1)

	go func() {
		defer close(items)
		defer close(errs)

		for seq := 1; seq <= spec.Count; seq++ {
			if spec.FailAt == seq {
				msg := spec.FailMsg
				if msg == "" {
					msg = fmt.Sprintf("injected upstream failure at item %d", seq)
				}
				select {
				case errs <- errors.New(msg):
				case <-ctx.Done():
				}
				return
			}

			if spec.ItemDelay > 0 {
				select {
				case <-s.clk.After(time.Duration(spec.ItemDelay) * time.Millisecond):
				case <-ctx.Done():
					return
				}
			}

			item := Item{Seq: seq, Payload: makePayload(seq, spec.ItemBytes)}
			select {
			case items <- item:
			case <-ctx.Done():
				return
			}
		}
	}()

	return items, errs
}

// makePayload builds a deterministic payload of n bytes derived from seq,
// so clients can verify content integrity, not just sequence numbers.
func makePayload(seq, n int) []byte {
	if n <= 0 {
		return []byte{}
	}
	p := make([]byte, n)
	base := byte('a' + (seq-1)%26)
	for i := range p {
		p[i] = base
	}
	return p
}
