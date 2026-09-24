package solver

import "testing"

func TestBinomial(t *testing.T) {
	cases := []struct {
		n, k int
		want int64
	}{
		{5, 0, 1}, {5, 5, 1}, {5, 1, 5}, {5, 4, 5},
		{8, 4, 70}, {10, 3, 120}, {20, 10, 184756}, {30, 8, 5852925},
	}
	for _, tc := range cases {
		if got := binomial(tc.n, tc.k, 1e15); got != tc.want {
			t.Errorf("C(%d,%d) = %d, want %d", tc.n, tc.k, got, tc.want)
		}
	}
	// Capping: ask with a tiny cap and verify the early return.
	if got := binomial(100, 10, 100); got != 100 {
		t.Errorf("capped binomial = %d, want 100", got)
	}
}

func TestSumRSmallest(t *testing.T) {
	v := []int64{5, 1, 9, 3, 7, 2, 8, 0, 4, 6}
	// Full slice sorted: 0,1,2,3,4,5,6,7,8,9
	cases := []struct {
		lo, hi, r int
		want      int64
	}{
		{0, 10, 1, 0},
		{0, 10, 3, 0 + 1 + 2},
		{0, 10, 5, 0 + 1 + 2 + 3 + 4},
		{0, 10, 10, 45},
		{2, 6, 2, 2 + 3}, // sub-slice [9,3,7,2] -> 2+3
		{0, 1, 1, 5},
		{0, 10, 0, 0},
	}
	for _, tc := range cases {
		if got := sumRSmallest(v, tc.lo, tc.hi, tc.r); got != tc.want {
			t.Errorf("sumRSmallest(v,%d,%d,%d) = %d, want %d", tc.lo, tc.hi, tc.r, got, tc.want)
		}
	}
}

// TestLBNeverOverestimates checks the pruning bound is admissible on random
// instances: at every partial selection it must not exceed the best completion
// cost achievable from that state. We verify admissibility against a local
// enumeration of completions.
func TestLBNeverOverestimates(t *testing.T) {
	// Small fixed matrix, enumerate every partial combination.
	n, k := 7, 4
	pool := [][]int64{
		{0, 2, 5, 1, 9, 3, 4},
		{2, 0, 1, 8, 2, 7, 1},
		{5, 1, 0, 3, 6, 2, 8},
		{1, 8, 3, 0, 4, 5, 2},
		{9, 2, 6, 4, 0, 1, 3},
		{3, 7, 2, 5, 1, 0, 6},
		{4, 1, 8, 2, 3, 6, 0},
	}

	mn := make([]int64, n)
	var sel []int
	var check func(start int)
	check = func(start int) {
		depth := len(sel)
		if depth == k {
			return
		}
		r := k - depth
		lb := sumRSmallest(mn, start, n, r)
		// Exact cheapest completion: enumerate the r-subsets of [start,n)
		// and their added cost (rows inside the completion plus cost into
		// the current selection).
		exact := enumerateMinCompletion(pool, sel, start, r)
		if lb > exact {
			t.Fatalf("LB %d overestimates exact completion cost %d at sel=%v start=%d", lb, exact, sel, start)
		}
		for i := start; i <= n-r; i++ {
			saved := make([]int64, n)
			for j := i + 1; j < n; j++ {
				saved[j] = mn[j]
				if depth == 0 || pool[i][j] < mn[j] {
					mn[j] = pool[i][j]
				}
			}
			sel = append(sel, i)
			check(i + 1)
			sel = sel[:len(sel)-1]
			for j := i + 1; j < n; j++ {
				mn[j] = saved[j]
			}
		}
	}
	check(0)
}

// enumerateMinCompletion computes, for a fixed partial selection, the minimum
// added cost of completing it with r distinct rows chosen from [start,n).
func enumerateMinCompletion(pool [][]int64, sel []int, start, r int) int64 {
	n := len(pool)
	best := int64(-1)
	var add []int
	var rec func(from int)
	rec = func(from int) {
		if len(add) == r {
			cost := int64(0)
			for a := 0; a < r; a++ {
				for _, s := range sel {
					cost += pool[s][add[a]]
				}
				for b := a + 1; b < r; b++ {
					cost += pool[add[a]][add[b]]
				}
			}
			if best == -1 || cost < best {
				best = cost
			}
			return
		}
		for i := from; i < n; i++ {
			// leave r-len(add)-1 rows after i
			if n-i < r-len(add) {
				break
			}
			add = append(add, i)
			rec(i + 1)
			add = add[:len(add)-1]
		}
	}
	rec(start)
	return best
}
