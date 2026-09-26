package retry

import (
	"math/rand"
	"time"
)

// Backoff computes exponential retry delays with optional jitter from an
// injectable random source.
type Backoff struct {
	Base       time.Duration // delay for the first retry
	Max        time.Duration // cap applied before jitter
	Multiplier float64       // growth factor per attempt (>= 1)
	// Rand, when non-nil, applies "equal jitter": the returned delay is
	// uniform in [d/2, d]. Inject a seeded *rand.Rand for determinism.
	Rand *rand.Rand
}

// Delay returns the wait before retry number attempt (1 = first retry).
func (b Backoff) Delay(attempt int) time.Duration {
	if attempt < 1 {
		attempt = 1
	}
	mult := b.Multiplier
	if mult < 1 {
		mult = 1
	}
	d := float64(b.Base)
	for i := 1; i < attempt; i++ {
		d *= mult
		if b.Max > 0 && d >= float64(b.Max) {
			d = float64(b.Max)
			break
		}
	}
	if b.Max > 0 && d > float64(b.Max) {
		d = float64(b.Max)
	}
	if b.Rand != nil && d > 0 {
		d = d/2 + d/2*b.Rand.Float64()
	}
	return time.Duration(d)
}
