package budget

import (
	"errors"
	"fmt"
	"math/bits"
	"sync"

	"tokenbudget/internal/clock"
)

// Validation bounds. Within these bounds every refill/wait intermediate value
// fits in unsigned 128 bits (num*elapsed <= 1e9*~3.2e18 < 2^94), and all token
// stocks fit in int64.
const (
	MaxRateNum  int64 = 1_000_000_000                      // at most 1e9 tokens per period
	MaxRateDen  int64 = 60 * 60 * 24 * 365 * 1_000_000_000 // slowest: 1 token / year (ns)
	MaxCapacity int64 = 1_000_000_000
)

// Errors returned by the budget layer.
var (
	ErrInvalidConfig   = errors.New("budget: invalid configuration")
	ErrTokensExceedCap = errors.New("budget: requested tokens exceed bucket capacity")
	ErrZeroTokens      = errors.New("budget: tokens must be positive")
	ErrUnknownTenant   = errors.New("budget: unknown tenant")
	ErrWaitTooLong     = errors.New("budget: required wait exceeds horizon")
)

// Rate is num tokens per den nanoseconds. A stopped bucket uses num == 0.
type Rate struct {
	Num int64 `json:"rate_num"`
	Den int64 `json:"rate_den_ns"`
}

// Config configures one bucket.
type Config struct {
	Rate          Rate   `json:"rate"`
	Capacity      int64  `json:"capacity"`
	InitialTokens *int64 `json:"initial_tokens,omitempty"` // default: full
}

// Validate checks structural validity.
func (c Config) Validate() error {
	if c.Capacity < 1 || c.Capacity > MaxCapacity {
		return fmt.Errorf("%w: capacity must be in [1,%d]", ErrInvalidConfig, MaxCapacity)
	}
	if c.Rate.Num < 0 || c.Rate.Num > MaxRateNum {
		return fmt.Errorf("%w: rate_num must be in [0,%d]", ErrInvalidConfig, MaxRateNum)
	}
	if c.Rate.Num > 0 && (c.Rate.Den < 1 || c.Rate.Den > MaxRateDen) {
		return fmt.Errorf("%w: positive rate requires rate_den_ns in [1,%d]", ErrInvalidConfig, MaxRateDen)
	}
	if c.InitialTokens != nil && (*c.InitialTokens < 0 || *c.InitialTokens > c.Capacity) {
		return fmt.Errorf("%w: initial_tokens must be in [0,capacity]", ErrInvalidConfig)
	}
	return nil
}

// Snapshot is the externally visible state of a bucket at one instant.
// Exact stock (in tokens) is Available + FracScaled/scale; FracScaled is the
// sub-token remainder projected onto the common nanosecond scale.
//
// AnchorNS is the instant to which Available/FracScaled apply. When
// AnchorNS > the snapshot instant, reservations have already consumed tokens
// that will only physically accrue by AnchorNS: Available is stock PROMISED
// AT AnchorNS, not spendable right now (CommittedUntil == AnchorNS).
type Snapshot struct {
	Rate       Rate
	Capacity   int64
	Available  int64
	FracScaled int64
	AnchorNS   int64
	NowNS      int64
}

// Committed reports whether future reservations are outstanding.
func (s Snapshot) Committed() bool { return s.AnchorNS > s.NowNS }

// bucket is a thread-safe lazy-refill token bucket using integer arithmetic
// only. It also supports firm reservations: `last` is the "accounting anchor"
// — the instant to which accrual and consumption have been materialized — and
// may legitimately lie in the FUTURE when tokens already promised to earlier
// reservations. Sub-token progress is carried in "numerator ticks":
//
//	accrued tokens since the anchor = (rem + num*elapsed) / den   (floor)
//	new rem                       = (rem + num*elapsed) mod den
//
// rem is in [0, den); it represents rem/den of one accrued-but-unminted token,
// a rate-independent fraction that survives rate changes (see applyConfig).
type bucket struct {
	mu sync.Mutex

	name     string
	num      int64 // tokens
	den      int64 // nanoseconds per period; 0 iff num == 0 (stopped)
	capacity int64
	avail    int64 // whole tokens at anchor `last`
	rem      int64 // sub-token accrual at anchor, [0, den)
	last     clock.Instant
}

func newBucket(name string, cfg Config, now clock.Instant) (*bucket, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	avail := cfg.Capacity
	if cfg.InitialTokens != nil {
		avail = *cfg.InitialTokens
	}
	den := cfg.Rate.Den
	if cfg.Rate.Num == 0 {
		den = 0 // stopped: no denominator
	}
	return &bucket{
		name:     name,
		num:      cfg.Rate.Num,
		den:      den,
		capacity: cfg.Capacity,
		avail:    avail,
		rem:      0,
		last:     now,
	}, nil
}

// settleLocked materializes accrual up to now WITHOUT consuming anything.
// Precondition: b.mu held. Monotonic time only; a reading at or before the
// anchor is ignored. The anchor may legitimately lie in the FUTURE (firm
// reservations already consumed tokens that accrue later); settle then leaves
// the promised state untouched rather than rewinding it.
func (b *bucket) settleLocked(now clock.Instant) {
	if now <= b.last {
		return
	}
	elapsed := uint64(int64(now) - int64(b.last))
	if b.num == 0 || elapsed == 0 {
		return // stopped or empty interval: anchor stays, nothing accrues
	}

	// v = rem + num*elapsed (128-bit).
	v := mul64(uint64(b.num), elapsed)
	var carry uint64
	v.lo, carry = bits.Add64(v.lo, uint64(b.rem), 0)
	v.hi += carry

	qhi, qlo, r := div128by64(v.hi, v.lo, uint64(b.den))
	gained := u128{hi: qhi, lo: qlo}
	b.rem = int64(r) // r < den <= MaxRateDen, fits int64

	avail128 := u128{lo: uint64(b.avail)}
	if gained.cmp(u128{}) != 0 {
		var c2 uint64
		avail128.lo, c2 = bits.Add64(avail128.lo, gained.lo, 0)
		avail128.hi = gained.hi + c2
	}
	cap128 := u128{lo: uint64(b.capacity)}
	if avail128.cmp(cap128) >= 0 {
		// Clamp at capacity; a full bucket discards pending fraction and
		// re-anchors at now. No stock above capacity ever exists.
		b.avail = b.capacity
		b.rem = 0
		b.last = now
		return
	}
	// avail128 < capacity <= MaxCapacity <= 1e9, hence hi == 0.
	b.avail = int64(avail128.lo)
	b.last = now
}

// tryLocked settles to now and deducts immediately. It returns remaining stock
// and true iff the tokens exist RIGHT NOW; on failure it mutates nothing
// (settle is a pure time projection), so non-blocking attempts never partially
// consume.
func (b *bucket) tryLocked(tokens int64, now clock.Instant) (remaining int64, ok bool) {
	b.settleLocked(now)
	if tokens > b.capacity {
		return b.avail, false
	}
	if b.avail >= tokens {
		b.avail -= tokens
		return b.avail, true
	}
	return b.avail, false
}

// readyLocked returns the earliest ABSOLUTE instant at which `tokens` can be
// served given current promises, WITHOUT committing. A future anchor means
// earlier reservations already queue behind now; the new reservation queues
// behind them.
func (b *bucket) readyLocked(tokens int64, now clock.Instant) (clock.Instant, error) {
	if tokens > b.capacity {
		return 0, ErrTokensExceedCap
	}
	eff := now
	if b.last > eff {
		eff = b.last // queue behind outstanding firm reservations
	}
	b.settleLocked(eff)
	if b.avail >= tokens {
		return eff, nil
	}
	if b.num == 0 {
		return 0, ErrWaitTooLong // no future supply
	}
	missing := tokens - b.avail
	// t = ceil((missing*den - rem) / num)
	need := mul64(uint64(missing), uint64(b.den))
	if uint64(b.rem) <= need.lo {
		need.lo -= uint64(b.rem)
	} else {
		need = need.sub(u128{lo: uint64(b.rem)}) // borrow across limbs
	}
	q, _ := div128by64Ceil(need.hi, need.lo, uint64(b.num))
	if q.hi != 0 || q.lo > uint64(1<<63-1)-uint64(eff) {
		return 0, ErrWaitTooLong
	}
	return clock.Instant(int64(eff) + int64(q.lo)), nil
}

// reserveLocked commits `tokens` at the ready instant previously obtained from
// readyLocked under the same lock: accrue to ready, deduct, and move the
// anchor there so subsequent reservations queue behind this one.
func (b *bucket) reserveLocked(tokens int64, ready clock.Instant) int64 {
	b.settleLocked(ready)
	if b.avail < tokens {
		b.avail = 0 // defensive; unreachable when ready came from readyLocked
		b.last = ready
		return 0
	}
	b.avail -= tokens
	b.last = ready
	return b.avail
}

// snapshotLocked returns the state after settling at now.
func (b *bucket) snapshotLocked(now clock.Instant) Snapshot {
	b.settleLocked(now)
	s := Snapshot{
		Rate:      Rate{Num: b.num, Den: b.den},
		Capacity:  b.capacity,
		Available: b.avail,
		AnchorNS:  int64(b.last),
		NowNS:     int64(now),
	}
	if b.den > 0 {
		// Project rem/den (fraction of a token) onto the common scale.
		prod := mul64(uint64(b.rem), uint64(scale))
		hi, lo, _ := div128by64(prod.hi, prod.lo, uint64(b.den))
		if hi == 0 {
			s.FracScaled = int64(lo)
		}
	}
	return s
}

// applyConfigLocked changes rate/capacity without ever creating stock:
//   - pending accrual is first materialized under the OLD rate;
//   - resuming a stopped bucket re-anchors at now (the stopped interval never
//     backfills);
//   - the sub-token fraction is conservatively converted to the new period
//     (floor: a rounding sliver may be lost, never invented);
//   - a smaller capacity clamps available stock;
//   - a larger capacity grants nothing.
func (b *bucket) applyConfigLocked(cfg Config, now clock.Instant) error {
	if err := cfg.Validate(); err != nil {
		return err
	}
	b.settleLocked(now)

	newDen := cfg.Rate.Den
	if cfg.Rate.Num == 0 {
		newDen = 0
	}
	wasStopped := b.num == 0

	switch {
	case wasStopped && newDen > 0:
		// Resume: no fraction survives a stopped interval; anchor at now so
		// the stopped period cannot be retroactively refilled.
		b.rem = 0
		b.last = now
	case b.den > 0 && newDen > 0 && b.rem > 0:
		// Preserve the rate-independent fraction rem/den of a token.
		prod := mul64(uint64(b.rem), uint64(newDen))
		_, lo, _ := div128by64(prod.hi, prod.lo, uint64(b.den))
		if lo < uint64(newDen) {
			b.rem = int64(lo)
		} else {
			b.rem = newDen - 1
		}
	default:
		b.rem = 0
	}

	b.num = cfg.Rate.Num
	b.den = newDen
	b.capacity = cfg.Capacity
	if b.avail > b.capacity {
		b.avail = b.capacity // shrinking capacity destroys excess, never adds
		b.rem = 0
		b.last = now
	}
	if b.num == 0 {
		b.rem = 0
	}
	return nil
}
