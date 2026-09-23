// Package accept 实现验收清单：以枚举和随机复放的方式验证
//
//  1. OR-Set 合并的交换律、结合律、幂等律；
//  2. 三副本 add/remove 快照在全部消息排列（6! = 720 种乱序）下收敛到同一结果；
//  3. 网络乱序 / 重复 / 丢包下并发新增被保留、重复同步依然收敛；
//  4. 墓碑回收的稳定性前提（安全回收不复活；遗漏滞后副本则复活）；
//  5. 模拟器确定性（同场景同 seed 两次运行逐字节一致）。
package accept

import (
	"encoding/json"
	"fmt"
	"math/rand/v2"

	"orset/crdt"
	"orset/sim"
)

// CaseResult 是单个验收项的结果。
type CaseResult struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Pass   bool   `json:"pass"`
	Detail string `json:"detail"`
}

// Report 是整套验收的输出。
type Report struct {
	AllPass bool         `json:"all_pass"`
	Cases   []CaseResult `json:"cases"`
}

// RunAll 执行全部验收项。
func RunAll() *Report {
	r := &Report{}
	r.Cases = []CaseResult{
		mergeLaws(),
		threeReplicaPermutations(),
		concurrentAddsRetained(),
		concurrentAddSurvivesObservedRemove(),
		duplicateSyncConverges(),
		dropStressConverges(),
		dropActuallyDiverges(),
		tombstoneGCStability(),
		determinismReplay(),
	}
	r.AllPass = true
	for _, c := range r.Cases {
		if !c.Pass {
			r.AllPass = false
		}
	}
	return r
}

// ---- 辅助 ----

func fail(id, name, format string, args ...interface{}) CaseResult {
	return CaseResult{ID: id, Name: name, Pass: false, Detail: fmt.Sprintf(format, args...)}
}

func pass(id, name, detail string) CaseResult {
	return CaseResult{ID: id, Name: name, Pass: true, Detail: detail}
}

func tag(origin string, counter uint64) crdt.UniqueTag {
	return crdt.UniqueTag{Origin: origin, Counter: counter}
}

// valuesEqual 忽略 nil/空切片差异比较有序字符串集合。
func valuesEqual(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// permutations 返回 [0,n) 的全部排列（Heap 算法）。
func permutations(n int) [][]int {
	var out [][]int
	a := make([]int, n)
	for i := range a {
		a[i] = i
	}
	var gen func(int)
	gen = func(k int) {
		if k == 1 {
			p := make([]int, n)
			copy(p, a)
			out = append(out, p)
			return
		}
		gen(k - 1)
		for i := 0; i < k-1; i++ {
			if k%2 == 0 {
				a[i], a[k-1] = a[k-1], a[i]
			} else {
				a[0], a[k-1] = a[k-1], a[0]
			}
			gen(k - 1)
		}
	}
	gen(n)
	return out
}

// ---- 1. 交换 / 结合 / 幂等 ----

// genState 用固定序列的 PRNG 生成随机 OR-Set 状态；元素域很小，
// 制造大量标签交集与墓碑重叠，增加暴露合并错误的概率。
func genState(origin string, rng *rand.Rand, ops int) *crdt.ORSet {
	s := crdt.New()
	counter := uint64(0)
	values := []string{"x", "y", "z"}
	for i := 0; i < ops; i++ {
		v := values[rng.IntN(len(values))]
		counter++
		_ = s.AddWithTag(v, tag(origin, counter))
		if rng.IntN(3) == 0 {
			s.RemoveObserved(v)
		}
	}
	return s
}

func mergeClone(a, b *crdt.ORSet) *crdt.ORSet {
	c := a.Clone()
	c.Merge(b)
	return c
}

func mergeLaws() CaseResult {
	const id, name = "A1", "合并交换律/结合律/幂等律"
	rng := rand.New(rand.NewPCG(42, 99))
	for round := 0; round < 300; round++ {
		a := genState("n1", rng, 12)
		b := genState("n2", rng, 12)
		c := genState("n3", rng, 12)

		// 幂等：a ⊔ a = a
		idemp := a.Clone()
		idemp.Merge(a.Clone())
		if !idemp.Equal(a) {
			return fail(id, name, "第 %d 轮: 幂等律失败", round)
		}
		// 交换：a ⊔ b = b ⊔ a
		if !mergeClone(a, b).Equal(mergeClone(b, a)) {
			return fail(id, name, "第 %d 轮: 交换律失败", round)
		}
		// 结合：(a ⊔ b) ⊔ c = a ⊔ (b ⊔ c)
		lhs := mergeClone(mergeClone(a, b), c)
		rhs := mergeClone(a, mergeClone(b, c))
		if !lhs.Equal(rhs) {
			return fail(id, name, "第 %d 轮: 结合律失败", round)
		}
	}
	return pass(id, name, "300 轮随机状态全部满足交换/结合/幂等")
}

// ---- 2. 三副本快照 6! 全排列乱序合并 ----

// snapshotSet 描述一组来自三副本的状态快照；全部快照以任意顺序
// 逐条并入空状态，结果必须相同。
type snapshotSet struct {
	expected []string
	snaps    []*crdt.ORSet
}

func buildSnapshotSets() []snapshotSet {
	// 集合 1：r1 添加 x；r2 添加 x 后又观察删除（含 r1 的标签）；r3 添加 y。
	s1 := crdt.New()
	_ = s1.AddWithTag("x", tag("r1", 1))

	s2 := crdt.New()
	_ = s2.AddWithTag("x", tag("r2", 1))
	s2.Merge(s1) // r2 观察到 r1 的添加
	s2.RemoveObserved("x")

	s3 := crdt.New()
	_ = s3.AddWithTag("y", tag("r3", 1))

	set1 := snapshotSet{expected: []string{"y"}, snaps: []*crdt.ORSet{s1, s2, s3}}

	// 集合 2：并发 add 与观察删除交错，外加不同元素，覆盖更多标签交叠。
	t1 := crdt.New()
	_ = t1.AddWithTag("x", tag("r1", 1))

	t2 := crdt.New()
	_ = t2.AddWithTag("x", tag("r2", 1))
	t2.Merge(t1)
	t2.RemoveObserved("x")

	t3 := crdt.New()
	_ = t3.AddWithTag("z", tag("r3", 1))
	_ = t3.AddWithTag("x", tag("r3", 2)) // 与删除并发，r2 删除时不可能观察到它

	// 再构造三张“后续增量快照”，模拟同一链路分批到达（含重复旧状态）。
	u1 := t1.Clone()
	_ = u1.AddWithTag("y", tag("r1", 2))
	u2 := t2.Clone()
	u2.Merge(s3)
	u3 := t3.Clone()
	u3.Merge(t2) // 含对 r2 删除墓碑的观察，但不含 r3#2 的墓碑

	set2 := snapshotSet{
		expected: []string{"x", "y", "z"}, // r3#2 存活 => x 保留
		snaps:    []*crdt.ORSet{t1, t2, t3, u1, u2, u3},
	}
	return []snapshotSet{set1, set2}
}

func threeReplicaPermutations() CaseResult {
	const id, name = "A2", "三副本乱序合并全排列枚举"
	total := 0
	for si, set := range buildSnapshotSets() {
		perms := permutations(len(set.snaps))
		// 期望结果 = 任意一次全量并集（以声明顺序为基准）。
		want := crdt.New()
		for _, s := range set.snaps {
			want.Merge(s)
		}
		if !valuesEqual(want.Values(), set.expected) {
			return fail(id, name, "集合 %d: 基准期望 %v，实际 %v", si+1, set.expected, want.Values())
		}
		for _, p := range perms {
			total++
			acc := crdt.New()
			for _, idx := range p {
				acc.Merge(set.snaps[idx])
			}
			if !acc.Equal(want) {
				return fail(id, name, "集合 %d: 排列 %v 收敛状态与基准不一致，值=%v", si+1, p, acc.Values())
			}
			if !valuesEqual(acc.Values(), set.expected) {
				return fail(id, name, "集合 %d: 排列 %v 值=%v，期望 %v", si+1, p, acc.Values(), set.expected)
			}
		}
	}
	return pass(id, name, fmt.Sprintf("两组快照共 %d 种消息顺序（含 6!=720）全部逐状态收敛", total))
}

// ---- 3. 并发新增保留（模拟器） ----

func concurrentAddsRetained() CaseResult {
	const id, name = "A3", "并发新增在乱序+重复网络下保留"
	sc := &sim.Scenario{
		Name:     "concurrent-adds",
		Replicas: []string{"r1", "r2", "r3"},
		Network:  sim.Network{DuplicateProb: 0.5, ReorderProb: 0.5, MinDelay: 1, MaxDelay: 8},
		Events: []sim.EventSpec{
			{At: 1, Kind: "add", Replica: "r1", Value: "x"},
			{At: 1, Kind: "add", Replica: "r2", Value: "y"},
			{At: 1, Kind: "add", Replica: "r3", Value: "z"},
			{At: 3, Kind: "gossip", Replica: "r1"},
			{At: 3, Kind: "gossip", Replica: "r2"},
			{At: 3, Kind: "gossip", Replica: "r3"},
			{At: 5, Kind: "gossip", Replica: "r1"},
			{At: 5, Kind: "gossip", Replica: "r2"},
			{At: 5, Kind: "gossip", Replica: "r3"},
			{At: 7, Kind: "gossip", Replica: "r1"},
			{At: 7, Kind: "gossip", Replica: "r2"},
			{At: 7, Kind: "gossip", Replica: "r3"},
		},
		ExpectedValues: []string{"x", "y", "z"},
	}
	for seed := uint64(1); seed <= 100; seed++ {
		sc.Seed = seed
		res := sim.Run(sc)
		if !res.Converged || !res.MatchesExpected {
			return fail(id, name, "seed=%d 未收敛到 {x y z}: converged=%v values=%v",
				seed, res.Converged, res.Nodes["r1"].Values)
		}
		if res.Stats.Reorders == 0 {
			return fail(id, name, "seed=%d 没有观察到乱序，场景未真正加压", seed)
		}
	}
	return pass(id, name, "100 个种子（50% 重复、50% 强制乱序）全部收敛到 {x y z}")
}

// ---- 4. 与删除并发的新增不被误删 ----

func concurrentAddSurvivesObservedRemove() CaseResult {
	const id, name = "A4", "观察删除只移除已观察标签，并发新增保留"
	sc := &sim.Scenario{
		Name:     "concurrent-add-vs-remove",
		Replicas: []string{"r1", "r2", "r3"},
		Network:  sim.Network{ReorderProb: 0.5, MinDelay: 1, MaxDelay: 2},
		Events: []sim.EventSpec{
			{At: 1, Kind: "add", Replica: "r1", Value: "x"},
			{At: 2, Kind: "sync", Replica: "r1", To: "r2"},     // r2 在 t=3..4 必观察到 x
			{At: 6, Kind: "remove", Replica: "r2", Value: "x"}, // 只墓碑化 r1#1
			{At: 6, Kind: "add", Replica: "r3", Value: "x"},    // 并发新增 r3#1
			{At: 9, Kind: "gossip", Replica: "r1"},
			{At: 9, Kind: "gossip", Replica: "r2"},
			{At: 9, Kind: "gossip", Replica: "r3"},
		},
		ExpectedValues: []string{"x"},
	}
	for seed := uint64(1); seed <= 100; seed++ {
		sc.Seed = seed
		res := sim.Run(sc)
		if !res.Converged || !res.MatchesExpected {
			return fail(id, name, "seed=%d: converged=%v, r1=%v r2=%v r3=%v（x 应被并发新增保留）",
				seed, res.Converged,
				res.Nodes["r1"].Values, res.Nodes["r2"].Values, res.Nodes["r3"].Values)
		}
	}
	// 单元级断言：r2 的删除确实只墓碑化了 r1#1，没有 r3#1。
	r1, r2, r3 := crdt.New(), crdt.New(), crdt.New()
	_ = r1.AddWithTag("x", tag("r1", 1))
	r2.Merge(r1)
	removed, _ := r2.RemoveObserved("x")
	_ = r3.AddWithTag("x", tag("r3", 1))
	if len(removed) != 1 || removed[0] != tag("r1", 1) {
		return fail(id, name, "删除墓碑集合错误: %v", removed)
	}
	r2.Merge(r3)
	if !r2.Contains("x") {
		return fail(id, name, "观察到并发新增后 x 应复活（存在未删除标签）")
	}
	return pass(id, name, "100 个种子全部保留并发新增；删除集合恰为已观察标签 {r1#1}")
}

// ---- 5. 重复同步（100% 复制投递）仍收敛且幂等 ----

func duplicateSyncConverges() CaseResult {
	const id, name = "A5", "重复同步收敛（幂等合并）"
	sc := &sim.Scenario{
		Name:     "duplicate-sync",
		Replicas: []string{"r1", "r2", "r3"},
		Network:  sim.Network{DuplicateProb: 1.0, ReorderProb: 0.5, MinDelay: 1, MaxDelay: 5},
		Events: []sim.EventSpec{
			{At: 1, Kind: "add", Replica: "r1", Value: "x"},
			{At: 1, Kind: "add", Replica: "r2", Value: "y"},
			{At: 2, Kind: "sync", Replica: "r1", To: "r2"},
			{At: 2, Kind: "sync", Replica: "r2", To: "r3"},
			// 两轮全量 gossip 兜底，使任意乱序/重复序列后三副本全状态一致。
			{At: 6, Kind: "gossip", Replica: "r1"},
			{At: 6, Kind: "gossip", Replica: "r2"},
			{At: 6, Kind: "gossip", Replica: "r3"},
			{At: 10, Kind: "gossip", Replica: "r1"},
			{At: 10, Kind: "gossip", Replica: "r2"},
			{At: 10, Kind: "gossip", Replica: "r3"},
		},
		ExpectedValues: []string{"x", "y"},
	}
	for seed := uint64(1); seed <= 50; seed++ {
		sc.Seed = seed
		res := sim.Run(sc)
		if !res.Converged || !res.MatchesExpected {
			return fail(id, name, "seed=%d: converged=%v values=%v", seed, res.Converged, res.Nodes["r1"].Values)
		}
		if res.Stats.DuplicatesMade == 0 || res.Stats.RedundantMerges == 0 {
			return fail(id, name, "seed=%d: 未观察到重复投递与冗余合并计数", seed)
		}
	}
	return pass(id, name, "50 个种子（100% 消息复制）全部收敛；冗余合并不改变状态")
}

// ---- 6a. 40% 丢包，多轮 gossip 最终收敛 ----

func dropStressConverges() CaseResult {
	const id, name = "A6a", "40% 丢包压力下多轮同步最终收敛"
	var events []sim.EventSpec
	events = append(events,
		sim.EventSpec{At: 1, Kind: "add", Replica: "r1", Value: "x"},
		sim.EventSpec{At: 1, Kind: "add", Replica: "r2", Value: "y"},
		sim.EventSpec{At: 1, Kind: "add", Replica: "r3", Value: "z"},
		sim.EventSpec{At: 2, Kind: "remove", Replica: "r2", Value: "y"}, // r2 删除自己未被别人观察的 y
	)
	for t := 5; t <= 5+14*5; t += 5 {
		events = append(events,
			sim.EventSpec{At: t, Kind: "gossip", Replica: "r1"},
			sim.EventSpec{At: t, Kind: "gossip", Replica: "r2"},
			sim.EventSpec{At: t, Kind: "gossip", Replica: "r3"},
		)
	}
	sc := &sim.Scenario{
		Name:     "drop-stress",
		Replicas: []string{"r1", "r2", "r3"},
		Network:  sim.Network{DropProb: 0.4, DuplicateProb: 0.2, ReorderProb: 0.2, MinDelay: 1, MaxDelay: 3},
		Events:   events,
		// y 被删除（无并发添加），x、z 保留。
		ExpectedValues: []string{"x", "z"},
	}
	for seed := uint64(1); seed <= 30; seed++ {
		sc.Seed = seed
		res := sim.Run(sc)
		if !res.Converged || !res.MatchesExpected {
			return fail(id, name, "seed=%d: converged=%v values=%v dropped=%d/%d",
				seed, res.Converged, res.Nodes["r1"].Values,
				res.Stats.MessagesDropped, res.Stats.MessagesSent)
		}
		if res.Stats.MessagesDropped == 0 {
			return fail(id, name, "seed=%d: 没有消息被丢弃，压力不足", seed)
		}
	}
	return pass(id, name, "30 个种子 × 15 轮 gossip（40% 丢包）全部最终收敛到 {x z}")
}

// ---- 6b. 丢包确实会导致暂时分叉（证明网络模型真实可丢包） ----

func dropActuallyDiverges() CaseResult {
	const id, name = "A6b", "丢包导致分叉、补传后重新收敛"
	// 选择若干种子，至少一个让首次 sync 被丢弃。
	foundDivergence := false
	var hitSeed uint64
	for seed := uint64(1); seed <= 200 && !foundDivergence; seed++ {
		sc := &sim.Scenario{
			Name:     "drop-divergence",
			Seed:     seed,
			Replicas: []string{"r1", "r2"},
			Network:  sim.Network{DropProb: 0.5, MinDelay: 1, MaxDelay: 2},
			Events: []sim.EventSpec{
				{At: 1, Kind: "add", Replica: "r1", Value: "x"},
				{At: 2, Kind: "sync", Replica: "r1", To: "r2"}, // 可能被丢
			},
		}
		res := sim.Run(sc)
		if res.Stats.MessagesDropped == 1 &&
			!valuesEqual(res.Nodes["r1"].Values, res.Nodes["r2"].Values) {
			foundDivergence = true
			hitSeed = seed
			// 同一调度补一轮 gossip 后应收敛。
			sc.Events = append(sc.Events, sim.EventSpec{At: 10, Kind: "sync", Replica: "r1", To: "r2"})
			res2 := sim.Run(sc)
			if !res2.Converged || !valuesEqual(res2.Nodes["r2"].Values, []string{"x"}) {
				return fail(id, name, "seed=%d 补传后仍未收敛: r2=%v", seed, res2.Nodes["r2"].Values)
			}
		}
	}
	if !foundDivergence {
		return fail(id, name, "200 个种子内未找到丢包分叉样本")
	}
	return pass(id, name, fmt.Sprintf("seed=%d 首次同步被丢弃时 r1={x} r2={}，补传后收敛到 {x}", hitSeed))
}

// ---- 7. 墓碑回收稳定性 ----

func tombstoneGCStability() CaseResult {
	const id, name = "A7", "墓碑回收稳定性前提"
	// 安全路径：所有将合并的副本都已观察墓碑后回收，有效值不变、仍收敛。
	r1 := crdt.New()
	_ = r1.AddWithTag("x", tag("r1", 1))
	r2 := r1.Clone()
	r3 := r1.Clone()
	r1.RemoveObserved("x") // 只有 r1 有墓碑
	if r1.SafeToReclaim(r2, r3) {
		return fail(id, name, "r2/r3 未观察墓碑时不应判定为可安全回收")
	}
	// 传播墓碑。
	r2.Merge(r1)
	r3.Merge(r1)
	if !r1.SafeToReclaim(r2, r3) {
		return fail(id, name, "全部副本观察墓碑后应可安全回收")
	}
	n1 := r1.ReclaimGC()
	r2.ReclaimGC()
	r3.ReclaimGC()
	if n1 != 1 || r1.Contains("x") || !r1.Equal(r2) || !r2.Equal(r3) {
		return fail(id, name, "安全回收后状态异常")
	}

	// 危险路径：遗漏一个滞后副本（持有 A 标签但无墓碑），回收导致复活。
	a := crdt.New()
	_ = a.AddWithTag("x", tag("a", 1))
	laggard := a.Clone() // 滞后：只知道添加，不知道删除
	a.RemoveObserved("x")
	// 错误地只对“在线”集合 {a} 确认前提就回收。
	if !a.SafeToReclaim() /* 没把 laggard 传入 */ {
		return fail(id, name, "空 others 应视为可回收（调用方承担完整性责任）")
	}
	a.ReclaimGC()
	if a.Contains("x") {
		return fail(id, name, "回收后 x 不应存活")
	}
	a.Merge(laggard) // 滞后副本回归合并
	if !a.Contains("x") {
		return fail(id, name, "滞后副本回归后 x 应复活——这正是不稳定回收的反例，未出现说明模型有误")
	}
	return pass(id, name, "全副本观察后回收安全且收敛；遗漏滞后副本会复活（反例已复现）")
}

// ---- 8. 确定性复放 ----

func determinismReplay() CaseResult {
	const id, name = "A8", "同场景同 seed 两次运行逐字节一致"
	sc := &sim.Scenario{
		Name:     "determinism",
		Seed:     12345,
		Replicas: []string{"r1", "r2", "r3"},
		Network:  sim.Network{DropProb: 0.3, DuplicateProb: 0.3, ReorderProb: 0.3, MinDelay: 1, MaxDelay: 7},
		Events: []sim.EventSpec{
			{At: 1, Kind: "add", Replica: "r1", Value: "x"},
			{At: 2, Kind: "add", Replica: "r2", Value: "y"},
			{At: 3, Kind: "remove", Replica: "r1", Value: "x"},
			{At: 4, Kind: "sync", Replica: "r1", To: "r2"},
			{At: 5, Kind: "gossip", Replica: "r2"},
			{At: 12, Kind: "gossip", Replica: "r1"},
			{At: 12, Kind: "gossip", Replica: "r3"},
		},
	}
	b1, _ := json.Marshal(sim.Run(sc))
	b2, _ := json.Marshal(sim.Run(sc))
	if string(b1) != string(b2) {
		return fail(id, name, "两次运行 JSON 输出不一致")
	}
	sc.Seed = 12346
	b3, _ := json.Marshal(sim.Run(sc))
	if string(b1) == string(b3) {
		return fail(id, name, "改变 seed 后输出不应完全相同")
	}
	return pass(id, name, "相同 seed 输出一致，不同 seed 输出不同")
}
