package budget

import (
	"time"

	"tokenbudget/internal/rational"
	"tokenbudget/internal/u128"
)

// MicroPerToken is the fixed-point scale: all token quantities are stored as
// int64 microtokens (1e6 per token). No float ever touches bucket state.
const MicroPerToken int64 = 1_000_000

// bucket is one token bucket. Its fields are guarded by Limiter.mu; a bucket
// is never touched without the limiter lock held.
type bucket struct {
	capacity int64         // microtokens, >= 0
	rate     rational.Rate // exact tokens/second
	tokens   int64         // currently available microtokens, 0 <= tokens <= capacity
	last     time.Time     // clock reading of the last refill

	// carry holds the sub-microtoken refill remainder: after computing
	// refill microtokens, the fraction (carryNum/carryDen) is kept and added
	// to the *next* interval's numerator so time like 1 token / 3 seconds
	// never loses or invents tokens at the microtoken boundary.
	carryNum uint64
	carryDen uint64 // 0 when no carry; always 1_000_000-scaled residue against rate.Den
}

func newBucket(cfg BucketConfig, now time.Time) *bucket {
	cap := cfg.CapacityMicro()
	return &bucket{
		capacity: cap,
		rate:     cfg.Rate,
		tokens:   cap, // buckets start full, i.e. permit an immediate burst
		last:     now,
	}
}

// refill advances the bucket to now using only elapsed monotonic time.
//
// It never fabricates stock: added microtokens are exactly
// floor((rate * elapsed + carried fraction) * 1e6), the new remainder is
// carried forward, and availability is capped at capacity (a full bucket
// wastes further refill, exactly as a standard token bucket does — a
// saturated bucket with no consumers produces no stock).
func (b *bucket) refill(now time.Time) {
	elapsed := now.Sub(b.last).Nanoseconds()
	if elapsed <= 0 {
		return
	}
	b.last = now
	if b.rate.IsZero() {
		b.carryNum = 0
		b.carryDen = 0
		return
	}
	// Being full at the START of the interval does not by itself discard
	// fractional credit: if a request consumes within the interval, refill
	// then fills the freed room. So compute the full accrued amount with
	// carry and clamp only the part that genuinely overflows capacity.

	// microtokens = rate.Num * elapsed_ns / (rate.Den * 1_000), plus the
	// carried fractional microtoken from last time.
	divisor := uint64(b.rate.Den) * 1_000
	total := u128.Mul64(uint64(b.rate.Num), uint64(elapsed))
	if b.carryNum != 0 && b.carryDen == divisor {
		total = u128.AddU64(total, b.carryNum)
	}
	q, rem := u128.DivU64(total, divisor)

	room := b.capacity - b.tokens
	if q.Hi != 0 || q.Lo >= uint64(room) {
		// Only the true overflow is discarded — this never removes tokens
		// that existed before the interval, and a perpetually-full bucket
		// gains nothing.
		b.tokens = b.capacity
		b.carryNum = 0
		b.carryDen = 0
		return
	}
	b.tokens += int64(q.Lo)
	if rem.Lo != 0 && rem.Hi == 0 {
		b.carryNum = rem.Lo
		b.carryDen = divisor
	} else {
		b.carryNum = 0
		b.carryDen = 0
	}
}

// timeUntil returns how long to wait until cost microtokens are available.
// Caller must have refilled first. The result is the exact nanosecond ceiling
// so a waiter never wakes one nanosecond too early or sleeps one too long.
func (b *bucket) timeUntil(now time.Time, cost int64) time.Duration {
	if b.tokens >= cost {
		return 0
	}
	if b.rate.IsZero() {
		return 1<<63 - 1 // effectively forever
	}
	// Nanoseconds needed for needMicro microtokens at rate num/den tok/s:
	//   dt_ns = needMicro * den * 1000 / num
	// The carried fraction (carryNum/carryDen microtokens, where
	// carryDen = den*1000) is a head start; in nanosecond numerator units it
	// contributes carryNum/num, i.e. numerator credit equal to carryNum.
	needMicro := uint64(cost - b.tokens)
	divisor := uint64(b.rate.Den) * 1000
	num128 := u128.MulU64(u128.From64(needMicro), divisor)
	if b.carryNum != 0 && b.carryDen == divisor {
		if num128.Lo <= b.carryNum && num128.Hi == 0 {
			// The carried fraction already reaches the boundary on the next
			// refill tick.
			return 1
		}
		num128 = u128.SubU64(num128, b.carryNum)
	}
	// Ceiling division so the waiter is never released before tokens exist.
	num128 = u128.AddU64(num128, uint64(b.rate.Num)-1)
	q, _ := u128.DivU64(num128, uint64(b.rate.Num))
	if q.FitsInt64() {
		d := time.Duration(q.Lo)
		if d <= 0 {
			return 1
		}
		return d
	}
	return 1<<63 - 1
}

// snapshot must be called after refill at the given time.
func (b *bucket) snapshot(now time.Time) *BucketSnapshot {
	return &BucketSnapshot{
		Available: rational.FormatMicroTokens(b.tokens),
		Capacity:  rational.FormatMicroTokens(b.capacity),
		Rate:      b.rate.String(),
		Last:      b.last,
	}
}
