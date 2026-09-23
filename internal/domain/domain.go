// Package domain contains the core types for the time-weighted price service.
//
// The service ingests discrete price samples (timestamp, integer price, source)
// and computes time-weighted average prices (TWAP) over fixed time windows by
// treating the price as a PIECEWISE-CONSTANT function of time: a sample at time
// t holds until the next effective sample. The simple arithmetic mean of
// samples is explicitly NOT the time-weighted mean.
package domain

import (
	"errors"
	"math/big"
	"time"
)

// Sample is one raw price observation.
//
//   - TS is the observation time (microsecond epoch).
//   - Price is an integer price in the smallest tradable unit.
//   - Source identifies the venue/feed that produced the observation.
type Sample struct {
	Symbol string
	TS     int64
	Price  int64
	Source string
}

// EffectiveSample is a raw sample after source-conflict resolution. If several
// sources report different prices for the same (symbol, ts), exactly one
// survives per timestamp (highest Priority, then lexicographically greatest
// Source as a deterministic tiebreak); Conflicting lists the rejected sources.
type EffectiveSample struct {
	TS          int64
	Price       int64
	Source      string
	Priority    int
	Conflicting []string
}

// WindowResult is the TWAP of one aligned, half-open window [StartUnix, EndUnix).
type WindowResult struct {
	Symbol    string
	WindowSec int64
	StartUnix int64 // inclusive
	EndUnix   int64 // exclusive

	// Integral of price over the covered part of the window, in
	// price*microseconds. CoveredUsec is the length it was integrated over.
	Integral    *big.Int // price*usec accumulated over covered segments
	CoveredUsec int64    // covered length in microseconds
	WindowUsec  int64    // always (EndUnix-StartUnix)*1e6

	// TWAP = Integral / CoveredUsec, rendered as a decimal with ScaleDigits
	// fractional digits (half-away-from-zero rounding). The exact rational is
	// also retained so callers never need to re-parse a float.
	TWAPNum    *big.Int // numerator
	TWAPDen    *big.Int // denominator (0 when CoveredUsec == 0)
	TWAPString string   // fixed-point decimal, "" when uncovered

	// Coverage is CoveredUsec/WindowUsec, rendered as a decimal in [0,1]
	// with 6 fractional digits.
	CoverageString string

	// Stale is true when coverage had to be cut short because the most recent
	// known price is older than the staleness horizon, or because the window
	// extends past "now". A stale TWAP is based only on genuinely observed
	// (or carried-forward within the horizon) price time.
	Stale bool

	// LastSampleUnix is the timestamp of the latest effective sample that
	// influenced the result (including the carry-in sample), or nil.
	LastSampleUnix *int64
	// Sources that actually contribute to the covered integral.
	Sources []string
	// Conflicts observed while resolving samples inside or carrying into the
	// window. Each entry is "sourceA,sourceB@<unix>".
	Conflicts []string
}

// Config holds the service-wide timing parameters.
type Config struct {
	WindowSec int64 // aligned window length, e.g. 60
	// LateToleranceSec: a sample with ts older than now-LateToleranceSec is
	// refused (late data accepted only within this horizon).
	LateToleranceSec int64
	// FutureGraceSec: samples slightly in the future (clock skew) are accepted.
	FutureGraceSec int64
	// StaleHorizonSec: a carried-forward price is trusted only for this long
	// after its sample time; beyond it the window is uncovered + stale.
	StaleHorizonSec int64
}

func DefaultConfig() Config {
	return Config{
		WindowSec:        60,
		LateToleranceSec: 300, // up to 5 minutes late
		FutureGraceSec:   2,
		StaleHorizonSec:  120,
	}
}

// Validate checks configuration invariants.
func (c Config) Validate() error {
	if c.WindowSec <= 0 {
		return errors.New("window seconds must be positive")
	}
	if c.LateToleranceSec < 0 || c.StaleHorizonSec < 0 || c.FutureGraceSec < 0 {
		return errors.New("timing parameters must not be negative")
	}
	return nil
}

const MicroPerSec = int64(time.Second / time.Microsecond) // 1_000_000

// AlignStart floors unixSec down to the window grid. Works for negative epoch
// values as well (Go division truncates toward zero, hence the correction).
func AlignStart(unixSec, windowSec int64) int64 {
	r := unixSec % windowSec
	if r < 0 {
		r += windowSec
	}
	return unixSec - r
}

// UnixToMicro converts seconds (possibly with a fractional part supplied as an
// integer microsecond epoch already; kept for readability of call sites).
func UnixMicro(t time.Time) int64 { return t.UnixMicro() }
