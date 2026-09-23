package twap

import (
	"twap-service/internal/domain"
)

// AffectedWindows returns grid-aligned window START TIMES IN MICROSECONDS that
// an upsert at sampleUs can possibly change.
//
// A sample at t defines the price on [t, nextSampleTs). Inserting or changing
// the sample at t therefore only changes the FORWARD interval [t, hi) where
// hi = min(next existing sample, t+staleHorizon, asOf):
//
//   - beyond the next sample the new observation never contributes;
//   - beyond the freshness horizon its carry is uncovered;
//   - past asOf nothing is observable.
//
// The interval [prev, t) is unaffected (it stays the previous event's price).
// For a correction to an already-existing timestamp we nonetheless extend one
// extra window backward as a CONSERVATIVE bound; recomputeWindow dedups via the
// content hash, so an unchanged window never gets a new version. Samples are
// never deleted, so no larger backward range is needed.
//
// The returned set may include a boundary window whose hash turns out
// unchanged; the caller skips version inserts on hash match.
func AffectedWindows(sampleUs, prevUs, nextUs int64, hadSample bool,
	windowSec, staleHorizonSec, asOfSec int64) []int64 {
	windowUs := windowSec * domain.MicroPerSec
	horizonUs := staleHorizonSec * domain.MicroPerSec
	asOfUs := asOfSec * domain.MicroPerSec

	lo := sampleUs
	if hadSample {
		// Conservative one-window-back extension for corrections.
		lo = sampleUs - windowUs
		if floored := sampleUs - horizonUs; floored > lo {
			lo = floored
		}
		if prevUs > lo {
			lo = prevUs
		}
	}

	hi := sampleUs + horizonUs
	if nextUs != 0 && nextUs < hi { // nextUs==0 means "no next sample"
		hi = nextUs
	}
	if asOfUs < hi {
		hi = asOfUs
	}
	// The price is effective AT t, so hi must be strictly greater than t for
	// any window to be affected.
	if hi <= sampleUs {
		hi = sampleUs + 1
	}

	first := domain.AlignStart(lo/1_000_000, windowSec) * 1_000_000
	// Largest window start whose half-open interval intersects [t, hi).
	hiIntersect := hi - 1
	last := domain.AlignStart(hiIntersect/1_000_000, windowSec) * 1_000_000
	if last < first {
		return nil
	}
	n := (last-first)/windowUs + 1
	out := make([]int64, 0, n)
	for s := first; s <= last; s += windowUs {
		out = append(out, s)
	}
	return out
}
