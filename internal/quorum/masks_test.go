package quorum

import (
	"math/bits"
	"testing"
)

// bruteSafety is the reference implementation: enumerate every subset pair by
// brute force and test disjointness directly. O(4^n) — only used on small
// inputs as the oracle for the DP-based implementation.
func bruteSafety(weights []int, rq, wq int) (wwSafe, rwSafe bool) {
	n := len(weights)
	size := 1 << n
	w := make([]int, size)
	for m := 1; m < size; m++ {
		bit := m & -m
		w[m] = w[m^bit] + weights[bits.TrailingZeros(uint(bit))]
	}
	isW := func(m uint32) bool { return w[m] >= wq }
	isR := func(m uint32) bool { return w[m] >= rq }
	wwSafe, rwSafe = true, true
	for a := 1; a < size; a++ {
		for b := 1; b < size; b++ {
			if uint32(a)&uint32(b) != 0 {
				continue
			}
			if isW(uint32(a)) && isW(uint32(b)) {
				wwSafe = false
			}
			if isR(uint32(a)) && isW(uint32(b)) {
				rwSafe = false
			}
		}
	}
	return
}

// bruteMinPair returns the minimum disjoint pair by the same ordering key,
// found by plain enumeration.
func bruteMinPair(weights []int, ta, tb int) (pair, bool) {
	n := len(weights)
	size := 1 << n
	w := make([]int, size)
	for m := 1; m < size; m++ {
		bit := m & -m
		w[m] = w[m^bit] + weights[bits.TrailingZeros(uint(bit))]
	}
	idx := &quorumIndex{weight: w, n: n}
	var best pair
	found := false
	for a := 1; a < size; a++ {
		if w[a] < ta {
			continue
		}
		for b := 1; b < size; b++ {
			if w[b] < tb || uint32(a)&uint32(b) != 0 {
				continue
			}
			cand := pair{uint32(a), uint32(b)}
			if !found || idx.pairLess(cand, best) {
				best, found = cand, true
			}
		}
	}
	return best, found
}

func TestDPMatchesBruteForce(t *testing.T) {
	weightsSet := [][]int{
		{1, 1, 1},
		{3, 2, 1},
		{1, 0, 2},
		{0, 0, 0},
		{5, 1, 1, 1},
		{2, 2, 2, 2},
		{1, 1, 1, 1, 1, 1},
		{0, 3, 0, 3},
		{7, 1, 4, 2, 6},
	}
	for _, weights := range weightsSet {
		total := 0
		for _, x := range weights {
			total += x
		}
		for rq := 1; rq <= total+1; rq++ {
			for wq := 1; wq <= total+1; wq++ {
				idx := buildIndex(weights, rq, wq)
				gotWW, gotRW := true, true
				if p, ok := idx.minDisjointPair(wq); ok {
					gotWW = false
					want, okB := bruteMinPair(weights, wq, wq)
					if !okB || p != want {
						t.Fatalf("weights=%v wq=%d min WW pair: got %v want %v", weights, wq, p, want)
					}
				}
				if p, ok := idx.minDisjointPair(rq); ok {
					gotRW = false
					want, okB := bruteMinPair(weights, rq, wq)
					if !okB || p != want {
						t.Fatalf("weights=%v rq=%d wq=%d min RW pair: got %v want %v", weights, rq, wq, p, want)
					}
				}
				wantWW, wantRW := bruteSafety(weights, rq, wq)
				if gotWW != wantWW || gotRW != wantRW {
					t.Fatalf("weights=%v rq=%d wq=%d: DP safety=(%v,%v) brute=(%v,%v)",
						weights, rq, wq, gotWW, gotRW, wantWW, wantRW)
				}
			}
		}
	}
}

// TestRandomCrossCheck exhaustively cross-validates the DP against brute force
// over thousands of random weight/threshold combinations (small n only).
func TestRandomCrossCheck(t *testing.T) {
	var state uint64 = 0xC0FFEE
	next := func() uint64 {
		state = state*6364136223846793005 + 1442695040888963407
		return state
	}
	for iter := 0; iter < 4000; iter++ {
		n := 1 + int(next()%5) // 1..5 nodes
		weights := make([]int, n)
		total := 0
		for i := range weights {
			weights[i] = int(next() % 4) // 0..3, zeros included
			total += weights[i]
		}
		if total == 0 {
			continue
		}
		rq := 1 + int(next()%(uint64(total)+1))
		wq := 1 + int(next()%(uint64(total)+1))
		idx := buildIndex(weights, rq, wq)
		_, wwBad := idx.minDisjointPair(wq)
		_, rwBad := idx.minDisjointPair(rq)
		bWW, bRW := bruteSafety(weights, rq, wq)
		if wwBad == bWW || rwBad == bRW {
			t.Fatalf("mismatch weights=%v rq=%d wq=%d: DP(ww=%v,rw=%v) brute(ww=%v,rw=%v)",
				weights, rq, wq, !wwBad, !rwBad, bWW, bRW)
		}
	}
}

// TestThresholdTheorems checks the standard sufficient conditions:
//
//	rq + wq > W  => read/write intersection
//	wq + wq > W  => write/write intersection
//
// and their tightness: equality allows a disjoint partition.
func TestThresholdTheorems(t *testing.T) {
	weights := []int{2, 3, 4, 1}
	total := 10
	idx := buildIndex(weights, total/2+1, total-total/2+1) // r=6,w=6: both sums > 10
	if _, ok := idx.minDisjointPair(6); ok {
		t.Fatal("r+w > W should guarantee RW intersection")
	}
	if _, ok := idx.minDisjointPair(6); ok {
		t.Fatal("2w > W should guarantee WW intersection")
	}
	idxEq := buildIndex(weights, 5, 5) // sums == W: disjoint partition possible
	if _, ok := idxEq.minDisjointPair(5); !ok {
		t.Fatal("r+w == W must allow disjoint quorums")
	}
}
