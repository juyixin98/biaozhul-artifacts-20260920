// Package twap implements time-weighted average price over fixed time
// windows. Prices are modelled as a right-continuous, piecewise-constant
// (step) function of time:
//
//	A sample (t, p) asserts that the price from t up to the next sample of
//	the same source is p (forward-fill). It says nothing about the past.
//
// The time-weighted average over a half-open window [wStart, wEnd) is
// therefore the integral of that step function divided by the *covered*
// duration — never the arithmetic mean of the sample values.
package twap

import (
	"errors"
	"math/big"
	"sort"
)

// Sample is one raw price observation from a source.
type Sample struct {
	TS     int64  // observation timestamp, microseconds since unix epoch
	Price  int64  // integer price
	Source string // data source identifier
}

// Segment describes one maximal sub-interval of the window on which the
// winning price is constant.
type Segment struct {
	Start    int64 `json:"start"`    // inclusive, window coordinates, micros
	End      int64 `json:"end"`      // exclusive, window coordinates, micros
	Price    int64 `json:"price"`    // winning price on [Start, End)
	Covered  bool  `json:"covered"`  // whether any source asserted a price
	Conflict bool  `json:"conflict"` // true if >=2 sources disagreed at Start
	Sources  int   `json:"sources"`  // number of sources asserting at Start
}

// Result is the TWAP evaluation of one window.
type Result struct {
	WindowStart int64 `json:"window_start"`
	WindowEnd   int64 `json:"window_end"`

	// TWAP = weightedPrice / coveredMicros, exact rational. TWAP is null
	// when coverage is zero.
	TWAPNum *big.Int `json:"-"`
	TWAPDen *big.Int `json:"-"`

	CoveredMicros int64    `json:"covered_micros"` // total duration with a known price
	CoverageNum   *big.Int `json:"-"`              // covered / window length
	CoverageDen   *big.Int

	WeightedSum *big.Int `json:"-"` // integral of price dt over covered parts

	ConflictCount int       `json:"conflict_count"` // in-window timestamps where sources disagreed
	SamplesUsed   int       `json:"samples_used"`   // samples t < wEnd that participated
	LastSampleTS  int64     `json:"last_sample_ts"` // most recent sample that shaped the window; 0 when none
	HasAnchor     bool      `json:"has_anchor"`     // a sample <= wStart existed
	Stale         bool      `json:"stale"`
	Segments      []Segment `json:"segments"`
}

// Params controls one evaluation.
type Params struct {
	WindowStart int64
	WindowEnd   int64
	Now         int64 // evaluation time (micros); samples at/after Now are not yet visible
	// StaleAfterMicros: the window is stale when the sample carrying the
	// price at the window's *effective end* is older than the effective
	// end by more than this. The effective end is min(wEnd, Now).
	StaleAfterMicros int64
}

var (
	errBadWindow = errors.New("window end must be greater than window start")
	errBadStale  = errors.New("stale-after must be non-negative")
)

// Compute evaluates the TWAP of [p.WindowStart, p.WindowEnd).
//
// Inputs are not required to be sorted and may contain multiple samples
// with the same timestamp, including duplicate (source, ts) rows.
//
// The algorithm merges sources as independent forward-fill streams:
//  1. only samples with ts < min(wEnd, Now) participate;
//  2. each source contributes its newest sample with ts <= a change
//     point (a sample at or before wStart is that source's anchor);
//  3. at every change point (wStart plus each distinct in-window ts) the
//     lexicographically smallest contributing source wins;
//  4. an anchor (a source's last sample before/at wStart) covers from
//     wStart itself — its forward-fill assertion was already in force
//     when the window began. With no anchor, the prefix up to the first
//     in-window sample is uncovered; it is never back-filled.
func Compute(samples []Sample, p Params) (*Result, error) {
	if p.WindowEnd <= p.WindowStart {
		return nil, errBadWindow
	}
	if p.StaleAfterMicros < 0 {
		return nil, errBadStale
	}

	// 1. Filter: no future prices. Samples with ts == Now are not visible
	// yet either (the observation at "now" may still be in flight); the
	// effective horizon is min(wEnd, Now).
	horizon := p.WindowEnd
	if p.Now != 0 && p.Now < horizon {
		horizon = p.Now
	}

	// Distinct timestamps that participate, mapped to their rows.
	groups := map[int64][]Sample{}
	var tsList []int64
	for _, s := range samples {
		if s.TS >= horizon || s.TS >= p.WindowEnd {
			continue
		}
		if _, ok := groups[s.TS]; !ok {
			tsList = append(tsList, s.TS)
		}
		groups[s.TS] = append(groups[s.TS], s)
	}
	sort.Slice(tsList, func(i, j int) bool { return tsList[i] < tsList[j] })

	// Deduplicate identical (source, ts) rows within each group; the last
	// row wins for that source (idempotent upsert semantics).
	for _, ts := range tsList {
		rows := groups[ts]
		bySource := map[string]Sample{}
		var order []string
		for _, s := range rows {
			if _, ok := bySource[s.Source]; !ok {
				order = append(order, s.Source)
			}
			bySource[s.Source] = s
		}
		sort.Strings(order)
		dedup := make([]Sample, 0, len(order))
		for _, src := range order {
			dedup = append(dedup, bySource[src])
		}
		groups[ts] = dedup
	}

	res := &Result{
		WindowStart: p.WindowStart,
		WindowEnd:   p.WindowEnd,
		TWAPNum:     big.NewInt(0),
		TWAPDen:     big.NewInt(0),
		CoverageNum: big.NewInt(0),
		CoverageDen: big.NewInt(p.WindowEnd - p.WindowStart),
		WeightedSum: big.NewInt(0),
		Segments:    []Segment{},
	}

	// active source -> latest sample at or before the current change point.
	active := map[string]Sample{}

	// change points: window start and every distinct ts strictly inside
	// [wStart, horizon). Groups strictly before wStart only seed anchors.
	idx := 0

	// Seed anchors: every source's most recent sample with ts <= wStart.
	// Several pre-window timestamps may contribute; keep the newest per
	// source.
	for idx < len(tsList) && tsList[idx] <= p.WindowStart {
		ts := tsList[idx]
		for _, s := range groups[ts] {
			active[s.Source] = s
		}
		idx++
	}

	effectiveEnd := p.WindowEnd
	if horizon < effectiveEnd {
		effectiveEnd = horizon
	}

	// emit closes the interval [cpStart, cpEnd) given the active set at
	// cpStart. It mutates res.
	emit := func(cpStart, cpEnd int64) {
		if cpEnd <= cpStart {
			return
		}
		if len(active) == 0 {
			res.Segments = append(res.Segments, Segment{
				Start: cpStart, End: cpEnd, Covered: false,
			})
			return
		}
		// Deterministic winner: smallest source name.
		var winner Sample
		first := true
		var conflict bool
		var prices = map[int64]struct{}{}
		for _, s := range active {
			prices[s.Price] = struct{}{}
			if first || s.Source < winner.Source {
				winner = s
				first = false
			}
		}
		if len(prices) >= 2 {
			conflict = true
		}
		if conflict {
			res.ConflictCount++
		}
		dur := cpEnd - cpStart
		seg := Segment{
			Start:    cpStart,
			End:      cpEnd,
			Price:    winner.Price,
			Covered:  true,
			Conflict: conflict,
			Sources:  len(active),
		}
		res.Segments = append(res.Segments, seg)
		res.CoveredMicros += dur
		res.WeightedSum.Add(res.WeightedSum, big.NewInt(winner.Price*dur))
	}

	cp := p.WindowStart
	// If no anchor exists, the prefix [wStart, firstInWindowTS) is
	// uncovered; the loop below handles that because active is empty at
	// cp == wStart.
	res.HasAnchor = len(active) > 0

	// Walk in-window change points.
	for idx < len(tsList) {
		ts := tsList[idx]
		if ts >= effectiveEnd {
			break
		}
		// Close [cp, ts) with the active set as of cp.
		emit(cp, ts)
		// Apply every source row at ts.
		for _, s := range groups[ts] {
			active[s.Source] = s
		}
		cp = ts
		idx++
	}
	// Tail to the effective end.
	emit(cp, effectiveEnd)

	// Count distinct samples used: every deduped row with ts < horizon
	// participates (as an anchor or as an in-window change point).
	nUsed := 0
	for _, ts := range tsList {
		if ts < effectiveEnd {
			nUsed += len(groups[ts])
		}
	}
	res.SamplesUsed = nUsed

	// Merge adjacent segments with identical state to keep output tight;
	// this is cosmetic only and must not change the math.
	res.Segments = mergeSegments(res.Segments)

	// Rationals.
	winLen := big.NewInt(p.WindowEnd - p.WindowStart)
	res.CoverageNum = big.NewInt(res.CoveredMicros)
	res.CoverageDen = winLen
	if res.CoveredMicros > 0 {
		num := new(big.Int).Set(res.WeightedSum)
		den := big.NewInt(res.CoveredMicros)
		g := new(big.Int).GCD(nil, nil, num, den)
		if g.Sign() > 0 {
			num.Quo(num, g)
			den.Quo(den, g)
		}
		res.TWAPNum, res.TWAPDen = num, den
	}

	// Stale: the price at the effective end is carried by the winner
	// source's latest sample; compare its age at the effective end.
	if effectiveEnd > p.WindowStart && len(active) > 0 {
		var winner Sample
		first := true
		for _, s := range active {
			if first || s.Source < winner.Source {
				winner = s
				first = false
			}
		}
		age := effectiveEnd - winner.TS
		res.LastSampleTS = winner.TS
		if age > p.StaleAfterMicros {
			res.Stale = true
		}
	} else {
		// No data at all at the effective end: definitionally stale.
		res.Stale = true
	}

	return res, nil
}

func mergeSegments(segs []Segment) []Segment {
	if len(segs) < 2 {
		return segs
	}
	out := segs[:1]
	for i := 1; i < len(segs); i++ {
		prev := &out[len(out)-1]
		cur := segs[i]
		if prev.End == cur.Start &&
			prev.Covered == cur.Covered &&
			prev.Price == cur.Price {
			prev.End = cur.End
			if cur.Conflict {
				prev.Conflict = true
			}
			if cur.Sources > prev.Sources {
				prev.Sources = cur.Sources
			}
			continue
		}
		out = append(out, cur)
	}
	return out
}

// FloatTWAP returns the TWAP as a float64, or nil when coverage is zero.
func (r *Result) FloatTWAP() *float64 {
	if r.CoveredMicros == 0 {
		return nil
	}
	f, _ := new(big.Float).Quo(
		new(big.Float).SetInt(r.TWAPNum),
		new(big.Float).SetInt(r.TWAPDen),
	).Float64()
	return &f
}

// FloatCoverage returns covered/window-length as a float64 in [0,1].
func (r *Result) FloatCoverage() float64 {
	f, _ := new(big.Float).Quo(
		new(big.Float).SetInt(r.CoverageNum),
		new(big.Float).SetInt(r.CoverageDen),
	).Float64()
	return f
}
