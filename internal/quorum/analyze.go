package quorum

import (
	"fmt"
	"sort"
)

// Analyze validates a configuration and produces an exhaustive Report.
func Analyze(cfg Config) *Report {
	v, errs := Validate(cfg)
	if errs != nil {
		return &Report{Valid: false, Errors: errs, ExhaustiveLimit: MaxNodes}
	}

	r := &Report{
		Valid:           true,
		Warnings:        v.warnings,
		Nodes:           v.names,
		Domains:         v.domains,
		NumNodes:        len(v.names),
		NumDomains:      len(v.domains),
		ReadQuorum:      v.cfg.ReadQuorum,
		WriteQuorum:     v.cfg.WriteQuorum,
		Exhaustive:      len(v.names) <= MaxNodes,
		ExhaustiveLimit: MaxNodes,
		TolerateDomains: v.cfg.TolerateDomains,
		RWSafe:          true,
		WWSafe:          true,
		Available:       true,
	}

	total := 0
	for _, wt := range v.weights {
		total += wt
	}
	r.TotalWeight = total

	idx := buildIndex(v.weights, v.cfg.ReadQuorum, v.cfg.WriteQuorum)

	// No-failure quorum enumeration (reference listings).
	rMasks, numR := idx.listQuorums(v.cfg.ReadQuorum)
	wMasks, numW := idx.listQuorums(v.cfg.WriteQuorum)
	r.NumReadQuorums = numR
	r.NumWriteQuorums = numW
	r.ReadQuorums = namesOfMasks(rMasks, v.names)
	r.WriteQuorums = namesOfMasks(wMasks, v.names)

	// Exact global intersection safety (independent of failure scenarios:
	// taking nodes away never creates new disjoint quorum pairs).
	if p, ok := idx.minDisjointPair(v.cfg.WriteQuorum); ok {
		r.WWSafe = false
		c := makeCounterexample("ww", p, idx, v, nil)
		r.MinimalWWCounterexample = c
		r.MinimalCounterexample = c
	}
	if p, ok := idx.minDisjointPair(v.cfg.ReadQuorum); ok {
		r.RWSafe = false
		c := makeCounterexample("rw", p, idx, v, nil)
		r.MinimalRWCounterexample = c
		if r.MinimalCounterexample == nil || pairLessReport(c, r.MinimalCounterexample) {
			r.MinimalCounterexample = c
		}
	}

	r.analyseDomains(v, idx)
	return r
}

// analyseDomains enumerates survivor scenarios after whole-domain outages up
// to the configured tolerance and checks quorum availability in each.
//
// Availability classification is O(k) per combination (domain weight sums);
// the exponential quorum enumeration is only run for the bounded number of
// scenarios included in the report.
func (r *Report) analyseDomains(v *validated, idx *quorumIndex) {
	t := v.cfg.TolerateDomains
	if t > len(v.domains) {
		t = len(v.domains)
	}
	r.TolerateDomains = t

	uni := uint32(1<<len(v.names) - 1)
	domainMask := make([]uint32, len(v.domains))
	domainWeight := make([]int, len(v.domains))
	for node, di := range v.domainOf {
		domainMask[di] |= 1 << uint(node)
		domainWeight[di] += v.weights[node]
	}

	type combo struct {
		domains    []int
		deadWeight int
	}
	var combos []combo
	var truncated bool
	for k := 1; k <= t && len(combos) < maxScenarios; k++ {
		enumerateCombos(len(v.domains), k, func(c []int) bool {
			if len(combos) >= maxScenarios {
				truncated = true
				return false
			}
			cp := &combos
			nc := combo{domains: append([]int(nil), c...)}
			for _, di := range c {
				nc.deadWeight += domainWeight[di]
			}
			*cp = append(*cp, nc)
			return true
		})
	}
	if truncated {
		r.Warnings = append(r.Warnings, Warning{
			Code:    "scenario_enumeration_truncated",
			Message: fmt.Sprintf("more than %d failure scenarios; enumeration truncated, availability result is conservative", maxScenarios),
		})
	}
	r.NumScenarios = len(combos) + 1 // includes the no-failure scenario

	// No-failure scenario (always listed first); global safety results apply.
	base := buildDetailedScenario(v, idx, uni, nil, false)
	base.WWSafe = r.WWSafe
	base.RWSafe = r.RWSafe
	base.Counterexample = r.MinimalCounterexample
	r.Scenarios = append(r.Scenarios, base)

	var failing, healthy []combo
	var firstUnavail []string
	for _, c := range combos {
		// Non-negative weights: quorum possible iff surviving total weight
		// reaches the threshold.
		readOK := r.TotalWeight-c.deadWeight >= r.ReadQuorum
		writeOK := r.TotalWeight-c.deadWeight >= r.WriteQuorum
		if readOK && writeOK {
			healthy = append(healthy, c)
			continue
		}
		failing = append(failing, c)
		if firstUnavail == nil {
			names := make([]string, len(c.domains))
			for i, di := range c.domains {
				names[i] = v.domains[di]
			}
			sort.Strings(names)
			firstUnavail = names
		}
	}

	if firstUnavail != nil {
		r.Available = false
		r.MinimalAvailabilityFailure = firstUnavail
	}

	listedFailing := failing
	failingTruncated := len(listedFailing) > 40
	if failingTruncated {
		listedFailing = listedFailing[:40]
	}
	listedHealthy := healthy
	healthyTruncated := len(listedHealthy) > maxHealthyScenarios
	if healthyTruncated {
		listedHealthy = listedHealthy[:maxHealthyScenarios]
	}
	r.ScenarioListTruncated = failingTruncated || healthyTruncated

	for _, c := range listedFailing {
		var dead uint32
		for _, di := range c.domains {
			dead |= domainMask[di]
		}
		r.Scenarios = append(r.Scenarios, buildDetailedScenario(v, idx, uni&^dead, c.domains, false))
	}
	for _, c := range listedHealthy {
		var dead uint32
		for _, di := range c.domains {
			dead |= domainMask[di]
		}
		r.Scenarios = append(r.Scenarios, buildDetailedScenario(v, idx, uni&^dead, c.domains, false))
	}

	// Reference scenario beyond the configured tolerance: every domain down.
	if t < len(v.domains) {
		s := buildDetailedScenario(v, idx, 0, allDomainIdx(len(v.domains)), true)
		r.Scenarios = append(r.Scenarios, s)
	}
}

func allDomainIdx(n int) []int {
	out := make([]int, n)
	for i := range out {
		out[i] = i
	}
	return out
}

// buildDetailedScenario runs the exponential enumeration for a single
// scenario: quorum listings/counts and restricted disjoint-pair search.
func buildDetailedScenario(v *validated, idx *quorumIndex, survivors uint32, failedDomainIdx []int, beyond bool) Scenario {
	readMasks, writeMasks, numR, numW := idx.scenarioQuorums(survivors)
	failed := make([]string, len(failedDomainIdx))
	for i, di := range failedDomainIdx {
		failed[i] = v.domains[di]
	}
	sort.Strings(failed)

	s := Scenario{
		FailedDomains:   failed,
		Survivors:       maskNames(survivors, v.names),
		SurvivorWeight:  idx.weight[survivors],
		ReadPossible:    numR > 0,
		WritePossible:   numW > 0,
		NumReadQuorums:  numR,
		NumWriteQuorums: numW,
		ReadQuorums:     namesOfMasks(readMasks, v.names),
		WriteQuorums:    namesOfMasks(writeMasks, v.names),
		WWSafe:          true,
		RWSafe:          true,
		BeyondTolerance: beyond,
	}

	// Only reachable quorum pairs count: failures can make an unsafe global
	// configuration safe in a scenario because one side cannot form.
	var minCE *Counterexample
	if p, ok := idx.minDisjointIn(survivors, v.cfg.WriteQuorum); ok {
		s.WWSafe = false
		minCE = makeCounterexample("ww", p, idx, v, failed)
	}
	if p, ok := idx.minDisjointIn(survivors, v.cfg.ReadQuorum); ok {
		s.RWSafe = false
		ce := makeCounterexample("rw", p, idx, v, failed)
		if minCE == nil || pairLessReport(ce, minCE) {
			minCE = ce
		}
	}
	s.Counterexample = minCE
	return s
}

func namesOfMasks(masks []uint32, names []string) [][]string {
	out := make([][]string, len(masks))
	for i, m := range masks {
		out[i] = maskNames(m, names)
	}
	return out
}

func makeCounterexample(kind string, p pair, idx *quorumIndex, v *validated, failed []string) *Counterexample {
	failedCopy := append([]string(nil), failed...)
	explanation := ""
	switch kind {
	case "ww":
		explanation = fmt.Sprintf(
			"two write quorums (each reaching write_quorum=%d) are node-disjoint; concurrent writes can both commit without a common node, so their ordering is undecidable",
			v.cfg.WriteQuorum)
	case "rw":
		explanation = fmt.Sprintf(
			"a read quorum (read_quorum=%d) and a write quorum (write_quorum=%d) are node-disjoint; a read can miss a committed write entirely (stale read)",
			v.cfg.ReadQuorum, v.cfg.WriteQuorum)
	}
	return &Counterexample{
		Kind:          kind,
		QuorumA:       maskNames(p.a, v.names),
		WeightA:       idx.weight[p.a],
		QuorumB:       maskNames(p.b, v.names),
		WeightB:       idx.weight[p.b],
		FailedDomains: failedCopy,
		Explanation:   explanation,
	}
}

// pairLessReport mirrors pair ordering between already-built counterexamples.
func pairLessReport(a, b *Counterexample) bool {
	sa := len(a.QuorumA) + len(a.QuorumB)
	sb := len(b.QuorumA) + len(b.QuorumB)
	if sa != sb {
		return sa < sb
	}
	wa, wb := a.WeightA+a.WeightB, b.WeightB+b.WeightB
	if wa != wb {
		return wa < wb
	}
	return false
}

func scenarioContains(v *validated, failedDomains, a, b []string) bool {
	down := map[string]bool{}
	for _, d := range failedDomains {
		for node, di := range v.domainOf {
			if v.domains[di] == d {
				down[v.names[node]] = true
			}
		}
	}
	for _, id := range append(append([]string{}, a...), b...) {
		if down[id] {
			return false
		}
	}
	return true
}

// enumerateCombos calls fn with every combination of k indices from [0,n) in
// lexicographic order. fn returning false stops enumeration.
func enumerateCombos(n, k int, fn func([]int) bool) {
	if k <= 0 || k > n {
		return
	}
	c := make([]int, k)
	for i := range c {
		c[i] = i
	}
	for {
		if !fn(c) {
			return
		}
		// next combination
		i := k - 1
		for ; i >= 0 && c[i] == n-k+i; i-- {
		}
		if i < 0 {
			return
		}
		c[i]++
		for j := i + 1; j < k; j++ {
			c[j] = c[j-1] + 1
		}
	}
}
