package quorum

import (
	"sort"
)

// Counterexample is a witness that a quorum configuration is unsafe: two
// quorums that can be formed simultaneously (after the listed domain
// failures) yet share no node.
type Counterexample struct {
	Kind          string   `json:"kind"` // "rw" or "ww"
	FailedDomains []string `json:"failed_domains"`
	QuorumA       []string `json:"quorum_a"`
	QuorumB       []string `json:"quorum_b"`
	WeightA       int      `json:"weight_a"`
	WeightB       int      `json:"weight_b"`
}

// ScenarioReport describes one failure scenario: which domains are down and
// which nodes remain available.
type ScenarioReport struct {
	FailedDomains       []string `json:"failed_domains"`
	Available           []string `json:"available"`
	AvailableWeight     int      `json:"available_weight"`
	ReadQuorumPossible  bool     `json:"read_quorum_possible"`
	WriteQuorumPossible bool     `json:"write_quorum_possible"`
}

// Report is the full result of Check.
type Report struct {
	TotalWeight      int              `json:"total_weight"`
	ReadThreshold    int              `json:"read_threshold"`
	WriteThreshold   int              `json:"write_threshold"`
	MaxFailedDomains int              `json:"max_failed_domains"`
	Warnings         []string         `json:"warnings,omitempty"`
	RWSafe           bool             `json:"rw_safe"`
	WWSafe           bool             `json:"ww_safe"`
	Safe             bool             `json:"safe"`
	RWCounterexample *Counterexample  `json:"rw_counterexample,omitempty"`
	WWCounterexample *Counterexample  `json:"ww_counterexample,omitempty"`
	Scenarios        []ScenarioReport `json:"scenarios"`
}

// Check validates cfg and exhaustively verifies read/write and write/write
// quorum intersection under every failure scenario up to MaxFailedDomains
// simultaneous domain failures. For unsafe configurations it returns the
// minimal counterexample (fewest nodes in the two disjoint quorums, then
// lexicographically smallest).
func Check(cfg *Config) (*Report, error) {
	warnings, err := cfg.Validate()
	if err != nil {
		return nil, err
	}
	nodes := cfg.SortedNodes()
	domains := cfg.Domains()
	rep := &Report{
		TotalWeight:      cfg.TotalWeight(),
		ReadThreshold:    cfg.ReadThreshold,
		WriteThreshold:   cfg.WriteThreshold,
		MaxFailedDomains: cfg.MaxFailedDomains,
		Warnings:         warnings,
		RWSafe:           true,
		WWSafe:           true,
	}
	maxFail := cfg.MaxFailedDomains
	if maxFail > len(domains) {
		maxFail = len(domains)
	}
	for k := 0; k <= maxFail; k++ {
		for _, failed := range combinations(domains, k) {
			avail := availableNodes(nodes, failed)
			sc := ScenarioReport{
				FailedDomains:   failed,
				Available:       nodeIDs(avail),
				AvailableWeight: weightOf(avail),
			}
			sc.ReadQuorumPossible = sc.AvailableWeight >= cfg.ReadThreshold
			sc.WriteQuorumPossible = sc.AvailableWeight >= cfg.WriteThreshold
			rep.Scenarios = append(rep.Scenarios, sc)

			if cx := findCounterexample(avail, cfg.ReadThreshold, cfg.WriteThreshold); cx != nil {
				rep.RWSafe = false
				cx.Kind = "rw"
				cx.FailedDomains = failed
				rep.RWCounterexample = minCounterexample(rep.RWCounterexample, cx)
			}
			if cx := findCounterexample(avail, cfg.WriteThreshold, cfg.WriteThreshold); cx != nil {
				rep.WWSafe = false
				cx.Kind = "ww"
				cx.FailedDomains = failed
				rep.WWCounterexample = minCounterexample(rep.WWCounterexample, cx)
			}
		}
	}
	rep.Safe = rep.RWSafe && rep.WWSafe
	return rep, nil
}

// availableNodes returns the sorted nodes whose domain is not in failed.
func availableNodes(nodes []Node, failed []string) []Node {
	down := make(map[string]bool, len(failed))
	for _, d := range failed {
		down[d] = true
	}
	var out []Node
	for _, n := range nodes {
		if !down[domainOf(n)] {
			out = append(out, n)
		}
	}
	return out
}

func nodeIDs(nodes []Node) []string {
	out := make([]string, len(nodes))
	for i, n := range nodes {
		out[i] = n.ID
	}
	return out
}

func weightOf(nodes []Node) int {
	w := 0
	for _, n := range nodes {
		w += n.Weight
	}
	return w
}

// combinations returns all k-element subsets of the sorted input, generated
// in lexicographic order. k == 0 yields a single empty slice.
func combinations(items []string, k int) [][]string {
	if k == 0 {
		return [][]string{{}}
	}
	if k > len(items) {
		return nil
	}
	var out [][]string
	idx := make([]int, k)
	for i := range idx {
		idx[i] = i
	}
	for {
		comb := make([]string, k)
		for i, j := range idx {
			comb[i] = items[j]
		}
		out = append(out, comb)
		// advance to next combination (lexicographic)
		i := k - 1
		for i >= 0 && idx[i] == len(items)-k+i {
			i--
		}
		if i < 0 {
			break
		}
		idx[i]++
		for j := i + 1; j < k; j++ {
			idx[j] = idx[j-1] + 1
		}
	}
	return out
}

// findCounterexample looks for two disjoint quorums within avail: quorum A
// meeting threshold tA and quorum B meeting threshold tB. It returns the
// minimal such pair (by |A|+|B|, then lexicographic), or nil if none exists.
//
// Because weights are non-negative, avail\A contains a quorum meeting tB iff
// weight(avail)-weight(A) >= tB.
func findCounterexample(avail []Node, tA, tB int) *Counterexample {
	total := weightOf(avail)
	if total < tA+tB {
		return nil // cannot even split the weight two ways
	}
	var best *Counterexample
	bestSize := 1 << 30
	n := len(avail)
	// Enumerate quorum A by increasing cardinality so we can stop early.
	for sizeA := 1; sizeA <= n && sizeA < bestSize; sizeA++ {
		forEachCombination(n, sizeA, func(idx []int) {
			if sizeA >= bestSize {
				return
			}
			wA := 0
			inA := make([]bool, n)
			for _, i := range idx {
				wA += avail[i].Weight
				inA[i] = true
			}
			if wA < tA || total-wA < tB {
				return
			}
			var rest []Node
			for i, nd := range avail {
				if !inA[i] {
					rest = append(rest, nd)
				}
			}
			b := minQuorum(rest, tB)
			if b == nil || sizeA+len(b) >= bestSize {
				return
			}
			cx := &Counterexample{
				QuorumA: nodeIDs(pick(avail, idx)),
				QuorumB: nodeIDs(b),
				WeightA: wA,
				WeightB: weightOf(b),
			}
			if best == nil || lessPair(cx, best) {
				best = cx
				bestSize = len(cx.QuorumA) + len(cx.QuorumB)
			}
		})
	}
	return best
}

// minQuorum returns a minimum-cardinality subset of nodes whose weight meets
// threshold (lexicographically smallest among ties), or nil if impossible.
// Zero-weight nodes are never part of a minimal quorum.
func minQuorum(nodes []Node, threshold int) []Node {
	if weightOf(nodes) < threshold {
		return nil
	}
	for size := 1; size <= len(nodes); size++ {
		var best []Node
		forEachCombination(len(nodes), size, func(idx []int) {
			if best != nil {
				return // already found at this cardinality; combos are lex-ordered
			}
			w := 0
			for _, i := range idx {
				w += nodes[i].Weight
			}
			if w >= threshold {
				best = pick(nodes, idx)
			}
		})
		if best != nil {
			return best
		}
	}
	return nil
}

func pick(nodes []Node, idx []int) []Node {
	out := make([]Node, len(idx))
	for i, j := range idx {
		out[i] = nodes[j]
	}
	return out
}

// forEachCombination calls fn with every size-k index combination of
// [0..n), in lexicographic order.
func forEachCombination(n, k int, fn func(idx []int)) {
	idx := make([]int, k)
	for i := range idx {
		idx[i] = i
	}
	for {
		fn(idx)
		i := k - 1
		for i >= 0 && idx[i] == n-k+i {
			i--
		}
		if i < 0 {
			return
		}
		idx[i]++
		for j := i + 1; j < k; j++ {
			idx[j] = idx[j-1] + 1
		}
	}
}

// lessPair orders counterexamples by total size then lexicographically.
func lessPair(a, b *Counterexample) bool {
	sa, sb := len(a.QuorumA)+len(a.QuorumB), len(b.QuorumA)+len(b.QuorumB)
	if sa != sb {
		return sa < sb
	}
	ja, jb := joinIDs(a), joinIDs(b)
	return ja < jb
}

func joinIDs(cx *Counterexample) string {
	s := ""
	for _, id := range cx.QuorumA {
		s += id + ","
	}
	s += "|"
	for _, id := range cx.QuorumB {
		s += id + ","
	}
	return s
}

func minCounterexample(cur, next *Counterexample) *Counterexample {
	if cur == nil || lessPair(next, cur) {
		return next
	}
	return cur
}

// EnumerateQuorums lists all minimal (by inclusion) quorums of the available
// nodes in each failure scenario, for the given threshold. Exported for the
// CLI `enum` command and tests.
func EnumerateQuorums(cfg *Config, threshold int) (map[string][][]string, error) {
	if _, err := cfg.Validate(); err != nil {
		return nil, err
	}
	nodes := cfg.SortedNodes()
	domains := cfg.Domains()
	maxFail := cfg.MaxFailedDomains
	if maxFail > len(domains) {
		maxFail = len(domains)
	}
	out := map[string][][]string{}
	for k := 0; k <= maxFail; k++ {
		for _, failed := range combinations(domains, k) {
			avail := availableNodes(nodes, failed)
			key := scenarioKey(failed)
			out[key] = minimalQuorums(avail, threshold)
		}
	}
	return out, nil
}

func scenarioKey(failed []string) string {
	if len(failed) == 0 {
		return "no failures"
	}
	s := "down: "
	for i, d := range failed {
		if i > 0 {
			s += ","
		}
		s += d
	}
	return s
}

// minimalQuorums returns all inclusion-minimal subsets of nodes meeting the
// threshold, each as sorted IDs, the list itself sorted lexicographically.
func minimalQuorums(nodes []Node, threshold int) [][]string {
	var out [][]string
	for size := 1; size <= len(nodes); size++ {
		foundAtSize := false
		forEachCombination(len(nodes), size, func(idx []int) {
			w := 0
			for _, i := range idx {
				w += nodes[i].Weight
			}
			if w < threshold {
				return
			}
			// inclusion-minimal: no proper subset may meet the threshold.
			// Since we iterate by increasing size, it suffices that removing
			// any single member drops below the threshold.
			minimal := true
			for _, i := range idx {
				if w-nodes[i].Weight >= threshold {
					minimal = false
					break
				}
			}
			if minimal {
				foundAtSize = true
				out = append(out, nodeIDs(pick(nodes, idx)))
			}
		})
		_ = foundAtSize
	}
	sort.Slice(out, func(i, j int) bool {
		for k := 0; k < len(out[i]) && k < len(out[j]); k++ {
			if out[i][k] != out[j][k] {
				return out[i][k] < out[j][k]
			}
		}
		return len(out[i]) < len(out[j])
	})
	return out
}
