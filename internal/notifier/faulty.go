package notifier

import (
	"context"
)

// Notify applies fault policy then appends. Failures armed as n > 0 fail
// exactly n calls; n < 0 fails every call until disarmed; 0 always succeeds.
func (f *FaultyNotifier) Notify(ctx context.Context, e Event) error {
	if f.Latency > 0 {
		f.clk.Sleep(f.Latency)
	}
	for {
		n := f.failures.Load()
		switch {
		case n < 0:
			return ErrInjected // fail forever
		case n == 0:
			f.log.Append(e)
			return nil
		default:
			// Claim one of the n remaining failures. Losers of the CAS race
			// re-read the counter, so the total number of failures stays exact.
			if f.failures.CompareAndSwap(n, n-1) {
				return ErrInjected
			}
		}
	}
}

// Disarm turns injected failures off.
func (f *FaultyNotifier) Disarm() { f.failures.Store(0) }

// Remaining reports how many armed failures are left (-1 = infinite).
func (f *FaultyNotifier) Remaining() int32 { return f.failures.Load() }
