package solver

import "math"

// binomial computes C(n,k), capped at cap (returns cap early when the result
// is known to reach it). Computed in float64 because values may exceed int64
// for large pools; callers only need threshold comparisons and small exact
// values. n and k are expected non-negative with k <= n.
func binomial(n, k int, cap float64) int64 {
	if k < 0 || k > n {
		return 0
	}
	if k == 0 || k == n {
		return 1
	}
	if k > n-k {
		k = n - k
	}
	var f float64 = 1
	for i := 1; i <= k; i++ {
		f = f * float64(n-k+i) / float64(i)
		if f >= cap {
			return int64(cap)
		}
	}
	return int64(f)
}

// exhaustive enumerates every k-combination of pool rows in lexicographic
// index order and returns the minimum-cost one. Ties keep the first
// (lexicographically smallest) combination found.
//
// States are counted in res for diagnostics.
func exhaustive(pool [][]int64, k int, res *Result) []int {
	n := len(pool)
	chosen := make([]int, k)
	var bestCost int64 = math.MaxInt64
	var best []int
	var states int64

	var dfs func(start, depth int, cost int64)
	dfs = func(start, depth int, cost int64) {
		if depth == k {
			states++
			if cost < bestCost {
				bestCost = cost
				best = append(best[:0], chosen...)
			}
			return
		}
		// Leave enough rows after i for the remaining (k-depth-1) picks.
		for i := start; i <= n-(k-depth); i++ {
			chosen[depth] = i
			add := int64(0)
			for p := 0; p < depth; p++ {
				add += pool[chosen[p]][i]
			}
			dfs(i+1, depth+1, cost+add)
		}
	}
	dfs(0, 0, 0)
	res.StatesExplored = states
	return best
}

// branchAndBound performs best-first-ish depth-first search over combinations
// in lexicographic order with an admissible lower bound for pruning.
//
// It returns the chosen pool indices, whether optimality was proven
// (budget not exhausted), and the number of node visits.
func branchAndBound(pool [][]int64, k int, budget int64) (chosen []int, optimal bool, states int64) {
	n := len(pool)

	bestCost, best := greedyUpperBound(pool, k)

	sel := make([]int, k)
	// mn[j] = minimum cost from j to any selected row; updated incrementally
	// along the DFS path.
	mn := make([]int64, n)

	var dfs func(depth, start int, cur int64)
	dfs = func(depth, start int, cur int64) {
		if states >= budget {
			return
		}
		states++

		if depth == k {
			if cur < bestCost {
				bestCost = cur
				best = append(best[:0], sel...)
			}
			return
		}

		// Admissible lower bound: cur + r smallest minima over not-yet-decided
		// rows (minimum cost from such a row into the current selection).
		r := k - depth
		lb := cur + sumRSmallest(mn, start, n, r)
		if lb >= bestCost {
			return
		}

		for i := start; i <= n-r; i++ {
			if states >= budget {
				return
			}
			sel[depth] = i
			add := int64(0)
			for p := 0; p < depth; p++ {
				add += pool[sel[p]][i]
			}

			// Update mn for rows strictly after i; save previous values so the
			// change can be rolled back when leaving this branch.
			saved := make([]int64, n)
			for j := i + 1; j < n; j++ {
				saved[j] = mn[j]
				if depth == 0 || pool[i][j] < mn[j] {
					mn[j] = pool[i][j]
				}
			}

			dfs(depth+1, i+1, cur+add)

			for j := i + 1; j < n; j++ {
				mn[j] = saved[j]
			}
		}
	}
	dfs(0, 0, 0)
	return best, states < budget, states
}

// greedyUpperBound builds an initial feasible best solution: start from the
// cheapest edge, then repeatedly add the pool row whose sum of costs into the
// current selection is minimal (ties: smallest index). It is used as the
// branch-and-bound incumbent so early pruning is effective.
func greedyUpperBound(pool [][]int64, k int) (int64, []int) {
	n := len(pool)
	if k == 1 {
		return 0, []int{0}
	}

	// Cheapest starting edge (lexicographic on ties).
	a, b := 0, 1
	best := pool[0][1]
	for i := 0; i < n; i++ {
		for j := i + 1; j < n; j++ {
			if pool[i][j] < best {
				best = pool[i][j]
				a, b = i, j
			}
		}
	}

	chosen := []int{a, b}
	used := make([]bool, n)
	used[a], used[b] = true, true
	cost := best

	for len(chosen) < k {
		var bestI = -1
		var bestAdd int64
		for i := 0; i < n; i++ {
			if used[i] {
				continue
			}
			add := int64(0)
			for _, p := range chosen {
				add += pool[p][i]
			}
			if bestI == -1 || add < bestAdd {
				bestAdd = add
				bestI = i
			}
		}
		used[bestI] = true
		chosen = append(chosen, bestI)
		cost += bestAdd
	}

	// Normalize to ascending indices so downstream pair generation is stable.
	for i := 1; i < len(chosen); i++ {
		for j := i; j > 0 && chosen[j-1] > chosen[j]; j-- {
			chosen[j-1], chosen[j] = chosen[j], chosen[j-1]
		}
	}
	return cost, chosen
}

// sumRSmallest returns the sum of the r smallest values in mn[lo:hi].
// There are always at least r values (callers guarantee this when pruning);
// r == 0 yields 0. It keeps the r smallest values in a small max-heap
// (best[0] is the largest of the kept values), linear in the scan range.
func sumRSmallest(mn []int64, lo, hi, r int) int64 {
	if r == 0 {
		return 0
	}
	best := make([]int64, 0, r) // max-heap of the r smallest values seen
	for j := lo; j < hi; j++ {
		v := mn[j]
		if len(best) < r {
			// Insert v and sift up.
			best = append(best, v)
			i := len(best) - 1
			for i > 0 {
				p := (i - 1) / 2
				if best[p] >= best[i] {
					break
				}
				best[p], best[i] = best[i], best[p]
				i = p
			}
		} else if v < best[0] {
			// Replace the heap maximum, then sift down.
			best[0] = v
			i := 0
			for {
				l, rr := 2*i+1, 2*i+2
				m := i
				if l < len(best) && best[l] > best[m] {
					m = l
				}
				if rr < len(best) && best[rr] > best[m] {
					m = rr
				}
				if m == i {
					break
				}
				best[i], best[m] = best[m], best[i]
				i = m
			}
		}
	}
	var sum int64
	for _, v := range best {
		sum += v
	}
	return sum
}
