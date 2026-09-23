package twap

import (
	"fmt"
	"math/big"
	"sort"

	"twap-service/internal/domain"
)

// ResolveEffective collapses raw samples that share a timestamp.
//
// Semantics:
//   - The price function is piecewise constant; at a given timestamp exactly
//     one price can be effective. Multiple sources at the same ts is a
//     "source conflict".
//   - In "priority" mode the source with the highest Priority wins; ties are
//     broken deterministically by the lexicographically greatest source name.
//     Equal-price reports from other sources are merged (same information, no
//     conflict); different-price losers are recorded in Conflicting.
//   - "reject" mode is enforced at the API layer (HTTP 409 before insert);
//     this function still resolves deterministically so stored data never
//     depends on arrival order.
//
// priorities maps source name -> priority; unknown sources default to 0.
func ResolveEffective(samples []domain.Sample, priorities map[string]int) []domain.EffectiveSample {
	groups := make(map[int64][]domain.Sample)
	var order []int64
	for _, s := range samples {
		if _, ok := groups[s.TS]; !ok {
			order = append(order, s.TS)
		}
		groups[s.TS] = append(groups[s.TS], s)
	}
	sort.Slice(order, func(i, j int) bool { return order[i] < order[j] })

	out := make([]domain.EffectiveSample, 0, len(order))
	for _, ts := range order {
		g := groups[ts]
		// Winner: highest priority, then greatest source name.
		sort.SliceStable(g, func(i, j int) bool {
			pi, pj := priorities[g[i].Source], priorities[g[j].Source]
			if pi != pj {
				return pi > pj
			}
			return g[i].Source > g[j].Source
		})
		winner := g[0]
		ev := domain.EffectiveSample{
			TS:       ts,
			Price:    winner.Price,
			Source:   winner.Source,
			Priority: priorities[winner.Source],
		}
		for _, loser := range g[1:] {
			if loser.Price != winner.Price {
				ev.Conflicting = append(ev.Conflicting, loser.Source)
			}
		}
		out = append(out, ev)
	}
	return out
}

type segment struct {
	ev        domain.EffectiveSample
	segStart  int64
	segEndRaw int64 // end implied by the next event or the window end
}

// ComputeWindow integrates the piecewise-constant price function over the
// half-open aligned window [startSec, endSec).
//
// Rules (see README "计算语义"):
//
//  1. A sample at t sets the price for [t, nextSampleTS). It is carried
//     BACKWARD into no interval: if no sample exists at or before the window
//     start, the prefix is uncovered rather than being filled by a later
//     sample ("no future prices fill history").
//  2. A carried price is trusted for staleHorizonSec after its sample time
//     only. Past that age the tail is uncovered; coverage stops and Stale is
//     set. A fresh in-window sample restarts coverage at its own timestamp.
//  3. Nothing past asOfSec (typically "now") is observable: future samples
//     are ignored and the window is capped at asOf. Windows reaching past
//     asOf are therefore stale.
//  4. A sample exactly on the end boundary belongs to the NEXT window; a
//     sample exactly on the start boundary belongs to THIS window.
func ComputeWindow(
	symbol string,
	startSec, endSec int64,
	eff []domain.EffectiveSample,
	asOfSec int64,
	staleHorizonSec int64,
) domain.WindowResult {
	startUs := startSec * domain.MicroPerSec
	endUs := endSec * domain.MicroPerSec
	asOfUs := asOfSec * domain.MicroPerSec
	horizonUs := staleHorizonSec * domain.MicroPerSec

	res := domain.WindowResult{
		Symbol:     symbol,
		WindowSec:  endSec - startSec,
		StartUnix:  startSec,
		EndUnix:    endSec,
		Integral:   new(big.Int),
		WindowUsec: endUs - startUs,
		TWAPNum:    new(big.Int),
		TWAPDen:    big.NewInt(0),
	}

	// Only events observable by asOf and strictly before the end boundary may
	// influence this half-open window.
	evs := make([]domain.EffectiveSample, 0, len(eff))
	var lastEventTs *int64
	for _, e := range eff {
		if e.TS >= endUs || e.TS > asOfUs {
			continue
		}
		evs = append(evs, e)
		t := e.TS
		lastEventTs = &t
	}
	if lastEventTs != nil {
		res.LastSampleUnix = lastEventTs
	}

	// Carry-in: latest event with ts <= window start (ts == start means the
	// price is effective exactly at the open, which belongs to us).
	var carry *domain.EffectiveSample
	idx := 0
	for i := range evs {
		if evs[i].TS <= startUs {
			carry = &evs[i]
			idx = i + 1
		} else {
			break
		}
	}

	segs := make([]segment, 0, len(evs)-idx+1)
	if carry != nil {
		rawEnd := endUs
		if idx < len(evs) {
			rawEnd = evs[idx].TS
		}
		segs = append(segs, segment{ev: *carry, segStart: startUs, segEndRaw: rawEnd})
	}
	// In-window events (start < ts < end).
	for j := idx; j < len(evs); j++ {
		rawEnd := endUs
		if j+1 < len(evs) {
			rawEnd = evs[j+1].TS
		}
		segs = append(segs, segment{ev: evs[j], segStart: evs[j].TS, segEndRaw: rawEnd})
	}

	srcSeen := map[string]bool{}
	addSource := func(s string) {
		if s != "" && !srcSeen[s] {
			srcSeen[s] = true
			res.Sources = append(res.Sources, s)
		}
	}
	cfSeen := map[string]bool{}
	addConflict := func(ev domain.EffectiveSample) {
		for _, l := range ev.Conflicting {
			key := fmt.Sprintf("%s,%s@%d", ev.Source, l, ev.TS/domain.MicroPerSec)
			if !cfSeen[key] {
				cfSeen[key] = true
				res.Conflicts = append(res.Conflicts, key)
			}
		}
	}

	for _, sg := range segs {
		// Price freshness cap relative to the event's own timestamp.
		freshEnd := sg.ev.TS + horizonUs
		segEnd := sg.segEndRaw
		if asOfUs < segEnd {
			segEnd = asOfUs
		}
		if freshEnd < segEnd {
			segEnd = freshEnd
		}
		// Conflicts are reported even if the segment is itself uncovered.
		addConflict(sg.ev)
		if segEnd > sg.segStart {
			dur := segEnd - sg.segStart
			res.Integral.Add(res.Integral, new(big.Int).Mul(
				big.NewInt(sg.ev.Price), big.NewInt(dur)))
			res.CoveredUsec += dur
			addSource(sg.ev.Source)
		}
	}

	if res.CoveredUsec > 0 {
		res.TWAPNum = new(big.Int).Set(res.Integral)
		res.TWAPDen = big.NewInt(res.CoveredUsec)
		res.TWAPString = ratToFixed(res.Integral, big.NewInt(res.CoveredUsec), 6)
	}
	res.CoverageString = coverageString(res.CoveredUsec, res.WindowUsec)
	// Any uncovered microsecond — leading gap (no carry-in), a freshness
	// hole, or a tail past asOf — makes the result stale.
	res.Stale = res.CoveredUsec < res.WindowUsec

	sort.Strings(res.Sources)
	sort.Strings(res.Conflicts)
	return res
}
