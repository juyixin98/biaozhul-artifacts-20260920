package quorum

import (
	"math/bits"
	"sort"
)

// quorumIndex holds precomputed subset tables over the full node universe.
//
// Because all weights are non-negative (enforced by Validate), "subset X
// contains a quorum of threshold T" is simply weight[X] >= T: if any subset
// of X reaches T then X does too. The interesting question is whether two
// *disjoint* quorums exist, which is answered with a subset-minimum DP.
type quorumIndex struct {
	n        int
	rq       int
	wq       int
	weight   []int
	zeroMask uint32
	// bestW[X] is a write quorum contained in X with the smallest key
	// (fewest nodes, then least weight, then smallest mask). 0 means no
	// write quorum is contained in X.
	bestW []uint32
}

func buildIndex(weights []int, rq, wq int) *quorumIndex {
	n := len(weights)
	size := 1 << n
	idx := &quorumIndex{n: n, rq: rq, wq: wq, weight: make([]int, size)}
	for i, w := range weights {
		if w == 0 {
			idx.zeroMask |= 1 << uint(i)
		}
	}
	w := idx.weight
	for m := 1; m < size; m++ {
		bit := m & -m
		w[m] = w[m^bit] + weights[bits.TrailingZeros(uint(bit))]
	}

	best := make([]uint32, size)
	for m := 1; m < size; m++ {
		if w[m] >= wq {
			best[m] = uint32(m)
		}
	}
	// Subset-lattice propagation: after processing bit i, best[X] holds the
	// minimum-key qualifying submask reachable by clearing any subset of the
	// first i bits. O(n * 2^n).
	for i := 0; i < n; i++ {
		bit := 1 << i
		for m := 0; m < size; m++ {
			if m&bit != 0 {
				if c := best[m^bit]; c != 0 && (best[m] == 0 || quorumLess(c, best[m], w)) {
					best[m] = c
				}
			}
		}
	}
	idx.bestW = best
	return idx
}

// quorumLess compares single-quorum masks by (node count, weight, mask).
func quorumLess(a, b uint32, w []int) bool {
	ca, cb := bits.OnesCount32(a), bits.OnesCount32(b)
	if ca != cb {
		return ca < cb
	}
	if w[a] != w[b] {
		return w[a] < w[b]
	}
	return a < b
}

type pair struct {
	a, b uint32
}

// pairKey orders counterexamples: fewest participating nodes first, then
// least total weight, then deterministic mask tie-breaks.
func (idx *quorumIndex) pairLess(p, q pair) bool {
	sa := bits.OnesCount32(p.a) + bits.OnesCount32(p.b)
	sb := bits.OnesCount32(q.a) + bits.OnesCount32(q.b)
	if sa != sb {
		return sa < sb
	}
	wa := idx.weight[p.a] + idx.weight[p.b]
	wb := idx.weight[q.a] + idx.weight[q.b]
	if wa != wb {
		return wa < wb
	}
	if p.a != q.a {
		return p.a < q.a
	}
	return p.b < q.b
}

// minDisjointPair returns the minimum-key disjoint quorum pair of the two
// threshold families (rqFamily for side A, write family for side B), or
// false if no disjoint pair exists.
//
// Enumerating every A is O(2^n); B is looked up in O(1) via bestW over the
// complement of A, so the search over all pairs is exact without an O(4^n)
// pairwise comparison.
func (idx *quorumIndex) minDisjointPair(aThreshold int) (pair, bool) {
	var best pair
	found := false
	uni := uint32(1<<idx.n - 1)
	for m := 1; m < len(idx.weight); m++ {
		if idx.weight[m] < aThreshold {
			continue
		}
		a := uint32(m)
		b := idx.bestW[uni^a]
		if b == 0 {
			continue
		}
		cand := pair{a, b}
		if !found || idx.pairLess(cand, best) {
			best, found = cand, true
		}
	}
	return best, found
}

// minDisjointIn is minDisjointPair restricted to a survivor set: both quorums
// must use surviving nodes. bestW over the full lattice is reused, queried on
// survivors\A so the returned B lies inside the survivor set.
func (idx *quorumIndex) minDisjointIn(survivors uint32, aThreshold int) (pair, bool) {
	var best pair
	found := false
	for a := survivors; a != 0; a = (a - 1) & survivors {
		if idx.weight[a] < aThreshold {
			continue
		}
		b := idx.bestW[survivors&^a]
		if b == 0 {
			continue
		}
		cand := pair{a, b}
		if !found || idx.pairLess(cand, best) {
			best, found = cand, true
		}
	}
	return best, found
}

// listQuorums returns canonical quorum masks reaching threshold, ordered by
// key, capped at maxListedSets. A zero-weight node is never part of a
// canonical quorum: it contributes nothing and would only create duplicate
// representations of the same reaching set. The exact total count of
// canonical quorums is returned alongside.
func (idx *quorumIndex) listQuorums(threshold int) (masks []uint32, total int) {
	for m := 1; m < len(idx.weight); m++ {
		if idx.weight[m] < threshold || uint32(m)&idx.zeroMask != 0 {
			continue
		}
		total++
		if len(masks) < maxListedSets {
			masks = append(masks, uint32(m))
		}
	}
	sort.Slice(masks, func(i, j int) bool { return quorumLess(masks[i], masks[j], idx.weight) })
	if total > maxListedSets {
		masks = masks[:maxListedSets]
	}
	return masks, total
}

// scenarioQuorums enumerates canonical quorums restricted to a survivor mask
// (zero-weight members excluded). A single submask pass counts read and write
// quorums and keeps the smallest listings.
func (idx *quorumIndex) scenarioQuorums(survivors uint32) (readMasks, writeMasks []uint32, numR, numW int) {
	for sub := survivors; sub != 0; sub = (sub - 1) & survivors {
		if sub&idx.zeroMask != 0 {
			continue
		}
		wt := idx.weight[sub]
		if wt >= idx.rq {
			numR++
			if len(readMasks) < maxListedSets {
				readMasks = append(readMasks, sub)
			}
		}
		if wt >= idx.wq {
			numW++
			if len(writeMasks) < maxListedSets {
				writeMasks = append(writeMasks, sub)
			}
		}
	}
	sort.Slice(readMasks, func(i, j int) bool { return quorumLess(readMasks[i], readMasks[j], idx.weight) })
	sort.Slice(writeMasks, func(i, j int) bool { return quorumLess(writeMasks[i], writeMasks[j], idx.weight) })
	return
}

func maskNames(mask uint32, names []string) []string {
	out := []string{}
	for i := 0; i < len(names); i++ {
		if mask&(1<<uint(i)) != 0 {
			out = append(out, names[i])
		}
	}
	return out
}
