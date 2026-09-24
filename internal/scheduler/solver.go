package scheduler

import (
	"sort"

	"github.com/example/gpu-placement/internal/topology"
)

// scoredSet 是一组被选中的设备下标（已升序）及其目标函数值。
type scoredSet struct {
	idx        []int
	cost       int
	samePairs  int
	crossPairs int
	evaluated  int  // 求解过程中累计评估的候选数（只在最终结果上回填）
	exhaustive bool // 是否由穷举算法给出（全局最优）
}

// score 计算设备集合的组内通信代价：
// 所有被选设备两两之间的 link cost 之和，并统计同/跨 NUMA 设备对数。
func scoreSet(cl *topology.Cluster, idx []int) (cost, same, cross int) {
	for a := 0; a < len(idx); a++ {
		for b := a + 1; b < len(idx); b++ {
			cost += cl.Cost(idx[a], idx[b])
			if cl.NUMANode(idx[a]) == cl.NUMANode(idx[b]) {
				same++
			} else {
				cross++
			}
		}
	}
	return cost, same, cross
}

// better 在两个同分候选之间做确定性裁决，保证穷举与启发式、
// 以及不同运行之间返回完全一致的结果。优先级：
//
//	通信代价升序 > 跨 NUMA 设备对数升序 > 设备 id 元组字典序升序
func better(cl *topology.Cluster, cand, best []int, cc, bc, crossC, crossB int) bool {
	if cc != bc {
		return cc < bc
	}
	if crossC != crossB {
		return crossC < crossB
	}
	for i := 0; i < len(cand) && i < len(best); i++ {
		ca, cb := cl.DeviceID(cand[i]), cl.DeviceID(best[i])
		if ca != cb {
			return ca < cb
		}
	}
	return false
}

// combCountLE 估算 C(n,k)，一旦计数超过 cap 就提前返回 cap+1，
// 避免大组合数溢出，也避免穷举规模失控。
func combCountLE(n, k, cap int) int {
	if k < 0 || k > n {
		return 0
	}
	if k > n-k {
		k = n - k
	}
	// 连乘并逐步约分，保持中间结果为整数。
	num := make([]int, k)
	for i := 0; i < k; i++ {
		num[i] = n - k + 1 + i
	}
	den := make([]int, k)
	for i := 1; i <= k; i++ {
		den[i-1] = i
	}
	result := 1
	for _, d := range den {
		// 从 num 中找能与 d 约分的元素（可能需要多个元素）。
		for d > 1 {
			canceled := false
			for j := range num {
				g := gcd(num[j], d)
				if g > 1 {
					num[j] /= g
					d /= g
					canceled = true
					if d == 1 {
						break
					}
				}
			}
			if !canceled {
				// 理论上不会发生（连续 k 个整数之积必被 k! 整除），
				// 保守地放大计数以触发启发式路径。
				return cap + 1
			}
		}
	}
	for _, v := range num {
		// 约分后 num 各因子之积恰为 C(n,k)，且中间积单调不减，
		// 一旦超过 cap 即可停止精确计算。
		if v != 0 && result > cap/v {
			return cap + 1
		}
		result *= v
		if result > cap {
			return cap + 1
		}
	}
	return result
}

func gcd(a, b int) int {
	for b != 0 {
		a, b = b, a%b
	}
	if a < 0 {
		return -a
	}
	return a
}

// solve 在 eligible（满足硬约束的设备下标集合）中选出 k 台，
// 最小化组内通信代价。调用方保证 0 < k <= len(eligible)。
func solve(cl *topology.Cluster, eligible []int, k int) scoredSet {
	els := append([]int(nil), eligible...)
	sort.Ints(els)

	if combCountLE(len(els), k, ExhaustiveCandidateLimit) <= ExhaustiveCandidateLimit {
		r := solveExhaustive(cl, els, k)
		r.exhaustive = true
		return r
	}
	return solveHeuristic(cl, els, k)
}

// solveExhaustive 枚举 eligible 的所有 C(m,k) 个子集，取全局最优。
func solveExhaustive(cl *topology.Cluster, eligible []int, k int) scoredSet {
	var best []int
	bestCost, bestCross := 0, 0
	evaluated := 0

	current := make([]int, 0, k)
	var recurse func(start int)
	recurse = func(start int) {
		if len(current) == k {
			idx := append([]int(nil), current...)
			cost, _, cross := scoreSet(cl, idx)
			evaluated++
			if best == nil || better(cl, idx, best, cost, bestCost, cross, bestCross) {
				best = idx
				bestCost, bestCross = cost, cross
			}
			return
		}
		// 剩余元素不足以补齐 k 个时剪枝。
		need := k - len(current)
		for i := start; i <= len(eligible)-need; i++ {
			current = append(current, eligible[i])
			recurse(i + 1)
			current = current[:len(current)-1]
		}
	}
	recurse(0)

	cost, same, cross := scoreSet(cl, best)
	return scoredSet{
		idx:        best,
		cost:       cost,
		samePairs:  same,
		crossPairs: cross,
		evaluated:  evaluated,
	}
}

// solveHeuristic 在规模过大时使用：medoid 贪心构造 + 1-swap 局部搜索。
// 不保证全局最优（结果中 Exhaustive=false），但对目标函数单调改进。
func solveHeuristic(cl *topology.Cluster, eligible []int, k int) scoredSet {
	evaluated := 0

	// ---- 构造阶段：选 medoid（到其余候选代价和最小的设备）作为种子，
	// 随后每轮加入“对当前集合新增代价最小”的设备。 ----
	seed := -1
	bestSeedCost := 0
	for _, i := range eligible {
		sum := 0
		for _, j := range eligible {
			sum += cl.Cost(i, j)
		}
		evaluated++
		if seed == -1 || sum < bestSeedCost ||
			(sum == bestSeedCost && cl.DeviceID(i) < cl.DeviceID(seed)) {
			seed, bestSeedCost = i, sum
		}
	}

	chosen := map[int]bool{seed: true}
	set := []int{seed}
	for len(set) < k {
		bestAdd, bestAddCost := -1, 0
		bestAddCross := 0
		for _, i := range eligible {
			if chosen[i] {
				continue
			}
			addCost, addCross := 0, 0
			for _, j := range set {
				addCost += cl.Cost(i, j)
				if cl.NUMANode(i) != cl.NUMANode(j) {
					addCross++
				}
			}
			evaluated++
			if bestAdd == -1 || addCost < bestAddCost ||
				(addCost == bestAddCost && addCross < bestAddCross) ||
				(addCost == bestAddCost && addCross == bestAddCross &&
					cl.DeviceID(i) < cl.DeviceID(bestAdd)) {
				bestAdd, bestAddCost, bestAddCross = i, addCost, addCross
			}
		}
		chosen[bestAdd] = true
		set = append(set, bestAdd)
		sort.Ints(set)
	}

	// ---- 1-swap 局部搜索：每轮扫描所有“成员 / 非成员”替换，
	// 采用第一个严格更优的交换，直到没有改进或触及安全上限。 ----
	maxSwaps := 10_000
	for swaps := 0; swaps < maxSwaps; swaps++ {
		curCost, _, curCross := scoreSet(cl, set)
		improved := false

		outSet := append([]int(nil), set...)
		sort.Ints(outSet)
		for _, out := range outSet {
			for _, in := range eligible {
				if chosen[in] {
					continue
				}
				cand := make([]int, 0, k)
				for _, x := range set {
					if x != out {
						cand = append(cand, x)
					}
				}
				cand = append(cand, in)
				sort.Ints(cand)
				cc, _, cross := scoreSet(cl, cand)
				evaluated++
				if better(cl, cand, set, cc, curCost, cross, curCross) {
					delete(chosen, out)
					chosen[in] = true
					set = cand
					curCost, curCross = cc, cross
					improved = true
					break
				}
			}
			if improved {
				break
			}
		}
		if !improved {
			break
		}
	}

	cost, same, cross := scoreSet(cl, set)
	return scoredSet{
		idx:        set,
		cost:       cost,
		samePairs:  same,
		crossPairs: cross,
		evaluated:  evaluated,
	}
}
