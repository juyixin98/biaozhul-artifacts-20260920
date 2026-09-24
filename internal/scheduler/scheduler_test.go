package scheduler

import (
	"math/big"
	"math/rand"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// helper: 创建一个 10000m CPU / 10000MiB 内存的调度器
func newTestScheduler(t *testing.T) *Scheduler {
	t.Helper()
	s, err := New(Resources{CPU: 10000, Mem: 10000})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return s
}

func mustTenant(t *testing.T, s *Scheduler, id string, weight *big.Rat, quota *Quota) {
	t.Helper()
	if err := s.CreateTenant(TenantSpec{ID: id, Weight: weight, Quota: quota}); err != nil {
		t.Fatalf("CreateTenant(%s): %v", id, err)
	}
}

func submit(t *testing.T, s *Scheduler, id, tenant string, cpu, mem int64) string {
	t.Helper()
	st, err := s.SubmitTask(TaskSpec{ID: id, Tenant: tenant, Demand: Resources{CPU: cpu, Mem: mem}})
	if err != nil {
		t.Fatalf("SubmitTask(%s): %v", id, err)
	}
	return st
}

func ratFrom(s string) *big.Rat {
	r, ok := new(big.Rat).SetString(s)
	if !ok {
		panic("bad rat: " + s)
	}
	return r
}

// assertConservation 验证资源守恒与不超卖：
//  1. 所有租户分配之和 == capacity - free；
//  2. 每维用量 <= 容量；
//  3. 每个租户分配 <= 自己的配额；
//  4. 快照里每个 running 任务的需求都计入属主分配，且总和严格相等。
func assertConservation(t *testing.T, s *Scheduler) {
	t.Helper()
	st := s.Snapshot()
	var usedCPU, usedMem int64
	for _, tn := range st.Tenants {
		if tn.Allocated.CPU < 0 || tn.Allocated.Mem < 0 {
			t.Fatalf("tenant %s has negative allocation %+v", tn.ID, tn.Allocated)
		}
		if tn.Allocated.CPU > tn.Quota.CPU || tn.Allocated.Mem > tn.Quota.Mem {
			t.Fatalf("tenant %s allocation %+v exceeds quota %+v", tn.ID, tn.Allocated, tn.Quota)
		}
		var sumCPU, sumMem int64
		for _, tk := range tn.Running {
			sumCPU += tk.CPU
			sumMem += tk.Mem
		}
		if sumCPU != tn.Allocated.CPU || sumMem != tn.Allocated.Mem {
			t.Fatalf("tenant %s allocated %+v != sum of running tasks (%d,%d), queued=%d",
				tn.ID, tn.Allocated, sumCPU, sumMem, len(tn.Queued))
		}
		usedCPU += tn.Allocated.CPU
		usedMem += tn.Allocated.Mem
	}
	if usedCPU > st.Capacity.CPU || usedMem > st.Capacity.Mem {
		t.Fatalf("cluster overcommitted: used (%d,%d) > capacity (%d,%d)",
			usedCPU, usedMem, st.Capacity.CPU, st.Capacity.Mem)
	}
	if usedCPU != st.Used.CPU || usedMem != st.Used.Mem {
		t.Fatalf("snapshot used %+v inconsistent with tenant sum (%d,%d)", st.Used, usedCPU, usedMem)
	}
	if st.Free.CPU != st.Capacity.CPU-usedCPU || st.Free.Mem != st.Capacity.Mem-usedMem {
		t.Fatalf("snapshot free %+v inconsistent", st.Free)
	}
}

// 场景：CPU 密集租户 alpha（6000cpu/1000mem 任务）与内存密集租户 beta
// （1000cpu/6000mem 任务）。验证主导份额选择与资源守恒。
func TestDRF_CPUAndMemoryIntensiveTenants(t *testing.T) {
	s := newTestScheduler(t)
	mustTenant(t, s, "alpha", nil, nil)
	mustTenant(t, s, "beta", nil, nil)

	// 先提交两个 beta 内存任务：第一个运行，第二个因内存不足排队
	if got := submit(t, s, "b1", "beta", 1000, 6000); got != StatusRunning {
		t.Fatalf("b1: want running, got %s", got)
	}
	if got := submit(t, s, "b2", "beta", 1000, 6000); got != StatusQueued {
		t.Fatalf("b2: want queued, got %s", got)
	}
	assertConservation(t, s)

	// 再提交 alpha 的 CPU 任务：beta 队首放不进，但 alpha 放得进，
	// 调度循环跳过 beta 选择份额最小的 alpha -> a1 运行
	if got := submit(t, s, "a1", "alpha", 6000, 1000); got != StatusRunning {
		t.Fatalf("a1: want running, got %s", got)
	}
	assertConservation(t, s)

	// a2：alpha 主导份额 0.6（CPU），beta 0.6（Mem），平局按 ID alpha<beta
	// 选 alpha，但其 6000 CPU 中集群只剩 3000 -> 排队（不超卖）
	if got := submit(t, s, "a2", "alpha", 6000, 1000); got != StatusQueued {
		t.Fatalf("a2: want queued, got %s", got)
	}
	st := s.Snapshot()
	if st.NumRunning != 2 || st.NumQueued != 2 {
		t.Fatalf("want 2 running / 2 queued, got %d/%d; state=%+v", st.NumRunning, st.NumQueued, st)
	}
	if st.Used.CPU != 7000 || st.Used.Mem != 7000 {
		t.Fatalf("want used (7000,7000), got (%d,%d)", st.Used.CPU, st.Used.Mem)
	}
	// 阻塞原因标注
	if v, _ := s.TaskSnapshot("a2"); v.BlockedReason != ReasonClusterCPU {
		t.Fatalf("a2 blocked reason = %q, want %q", v.BlockedReason, ReasonClusterCPU)
	}
	if v, _ := s.TaskSnapshot("b2"); v.BlockedReason != ReasonClusterMem {
		t.Fatalf("b2 blocked reason = %q, want %q", v.BlockedReason, ReasonClusterMem)
	}
	assertConservation(t, s)

	// 释放 b1：beta 主导份额降为 0，b2 立即被调度
	if err := s.ReleaseTask("b1"); err != nil {
		t.Fatalf("release b1: %v", err)
	}
	if v, _ := s.TaskSnapshot("b2"); v.Status != StatusRunning {
		t.Fatalf("after release b1, b2: want running, got %s", v.Status)
	}
	if v, _ := s.TaskSnapshot("a2"); v.Status != StatusQueued {
		t.Fatalf("a2 should still be queued, got %s", v.Status)
	}
	assertConservation(t, s)

	if err := s.ReleaseTask("b2"); err != nil {
		t.Fatalf("release b2: %v", err)
	}
	// 此时 a1 仍占 (6000,1000)，空闲只有 (4000,9000)，a2 需要 6000 CPU，
	// 必须继续排队（不能因为 beta 退出就让 a2 超卖放置）。
	if v, _ := s.TaskSnapshot("a2"); v.Status != StatusQueued {
		t.Fatalf("a2 must remain queued while a1 holds 6000 CPU, got %s", v.Status)
	}
	assertConservation(t, s)
	// 链式释放 a1 后 a2 才运行，由 TestDRF_ReleaseReschedulesChain 覆盖。
}

// 链式释放：b2 释放后 a1 仍占位，a2 仍排队；再释放 a1，a2 才运行。
func TestDRF_ReleaseReschedulesChain(t *testing.T) {
	s := newTestScheduler(t)
	mustTenant(t, s, "alpha", nil, nil)
	mustTenant(t, s, "beta", nil, nil)

	submit(t, s, "b1", "beta", 1000, 6000)
	submit(t, s, "b2", "beta", 1000, 6000) // queued
	submit(t, s, "a1", "alpha", 6000, 1000)
	submit(t, s, "a2", "alpha", 6000, 1000) // queued
	assertConservation(t, s)

	if err := s.ReleaseTask("b1"); err != nil {
		t.Fatal(err)
	}
	if v, _ := s.TaskSnapshot("b2"); v.Status != StatusRunning {
		t.Fatalf("b2 should run after b1 release, got %s", v.Status)
	}
	if v, _ := s.TaskSnapshot("a2"); v.Status != StatusQueued {
		t.Fatalf("a2 should remain queued while a1+b2 occupy 7000 CPU, got %s", v.Status)
	}
	assertConservation(t, s)

	if err := s.ReleaseTask("b2"); err != nil {
		t.Fatal(err)
	}
	// 释放 b2 后：只剩 a1(6000,1000)，空闲 (4000,9000)；a2 需要 6000 CPU，不够
	if v, _ := s.TaskSnapshot("a2"); v.Status != StatusQueued || v.BlockedReason != ReasonClusterCPU {
		t.Fatalf("a2 should be queued on cluster cpu, got status=%s reason=%q", v.Status, v.BlockedReason)
	}
	assertConservation(t, s)

	if err := s.ReleaseTask("a1"); err != nil {
		t.Fatal(err)
	}
	if v, _ := s.TaskSnapshot("a2"); v.Status != StatusRunning {
		t.Fatalf("a2 should run after a1 release, got %s (reason=%s)", v.Status, v.BlockedReason)
	}
	st := s.Snapshot()
	if st.NumQueued != 0 || st.NumRunning != 1 {
		t.Fatalf("want exactly 1 running / 0 queued, got %d/%d", st.NumRunning, st.NumQueued)
	}
	assertConservation(t, s)
}

// 确定性平局规则：份额相同按租户 ID 字典序；同租户严格 FIFO。
// 构造方式：filler 租户先占满集群，三个竞争租户各提交 3 个等量任务排队，
// 再逐个释放 filler —— 每次释放都在“所有队列非空”的同一次调度中决定胜负，
// 从而把平局规则与离散粒度的影响隔离出来。
func TestDRF_DeterministicTieBreak(t *testing.T) {
	s := newTestScheduler(t)
	mustTenant(t, s, "filler", nil, nil)
	mustTenant(t, s, "zeta", nil, nil)
	mustTenant(t, s, "alpha", nil, nil)
	mustTenant(t, s, "mid", nil, nil)

	// filler 三个任务恰好占满 (4000+3000+3000 = 10000)。
	submit(t, s, "f1", "filler", 4000, 4000)
	submit(t, s, "f2", "filler", 3000, 3000)
	submit(t, s, "f3", "filler", 3000, 3000)

	// 竞争租户各 3 个 (1000,1000) 任务，全部排队。
	for _, tn := range []string{"zeta", "alpha", "mid"} {
		for i := 1; i <= 3; i++ {
			submit(t, s, tn[0:1]+itoa(i), tn, 1000, 1000)
		}
	}
	st := s.Snapshot()
	if st.NumRunning != 3 || st.NumQueued != 9 {
		t.Fatalf("setup: want 3 filler running / 9 queued, got %d/%d", st.NumRunning, st.NumQueued)
	}

	// 释放 f1（4000）：同轮按 ID 序 alpha,mid,zeta,alpha 放置 4 个。
	if err := s.ReleaseTask("f1"); err != nil {
		t.Fatal(err)
	}
	st = s.Snapshot()
	counts := runningCounts(st)
	if counts["alpha"] != 2 || counts["mid"] != 1 || counts["zeta"] != 1 {
		t.Fatalf("after f1: want alpha=2 mid=1 zeta=1, got %v", counts)
	}
	// 剩余队列：mid 的 m2,m3 与 zeta 的 z2,z3；同租户顺序必须是 FIFO。
	midQ := tenantView(st, "mid").Queued
	zetaQ := tenantView(st, "zeta").Queued
	if len(midQ) != 2 || midQ[0].ID != "m2" || midQ[1].ID != "m3" {
		t.Fatalf("mid FIFO queue broken: %+v", midQ)
	}
	if len(zetaQ) != 2 || zetaQ[0].ID != "z2" || zetaQ[1].ID != "z3" {
		t.Fatalf("zeta FIFO queue broken: %+v", zetaQ)
	}
	assertConservation(t, s)

	// 释放 f2（3000）：当前份额 alpha=.2, mid=.1, zeta=.1，
	// mid<zeta（ID）先补到 .2，再 zeta，再平局 alpha 补第三个。
	if err := s.ReleaseTask("f2"); err != nil {
		t.Fatal(err)
	}
	st = s.Snapshot()
	counts = runningCounts(st)
	if counts["alpha"] != 3 || counts["mid"] != 2 || counts["zeta"] != 2 {
		t.Fatalf("after f2: want alpha=3 mid=2 zeta=2, got %v", counts)
	}

	// 释放 f3（3000）：mid、zeta 各补第三个；最终 9 个任务占 9000，
	// 每维各剩余 1000 —— 离散任务无法利用，形成资源孤岛（不超卖）。
	if err := s.ReleaseTask("f3"); err != nil {
		t.Fatal(err)
	}
	st = s.Snapshot()
	counts = runningCounts(st)
	if counts["alpha"] != 3 || counts["mid"] != 3 || counts["zeta"] != 3 {
		t.Fatalf("after f3: want 3/3/3, got %v", counts)
	}
	if st.NumQueued != 0 {
		t.Fatalf("all queues should drain, %d remain", st.NumQueued)
	}
	if st.Free.CPU != 1000 || st.Free.Mem != 1000 {
		t.Fatalf("want (1000,1000) stranded by indivisible tasks, got free %+v", st.Free)
	}
	assertConservation(t, s)
}

// 精确分数权重：beta 权重 1/3，alpha 权重 1。
// 稳态加权份额相等：share_alpha/1 == share_beta/(1/3)，即份额比 3:1。
// big.Rat 精确解析 "1/3" 这类无限循环小数；float64 会有表示误差。
func TestDRF_WeightedShareIsExact(t *testing.T) {
	s := newTestScheduler(t)
	mustTenant(t, s, "filler", nil, nil)
	mustTenant(t, s, "alpha", nil, nil)           // 权重 1
	mustTenant(t, s, "beta", ratFrom("1/3"), nil) // 权重 1/3

	// filler 先占 9000/9000，两个租户各提交 80 个 (100,1000) 小任务排队。
	submit(t, s, "f", "filler", 9000, 9000)
	for i := 0; i < 80; i++ {
		submit(t, s, id("a", i), "alpha", 100, 100)
		submit(t, s, id("b", i), "beta", 100, 100)
	}
	if err := s.ReleaseTask("f"); err != nil {
		t.Fatal(err)
	}
	st := s.Snapshot()
	a := tenantView(st, "alpha")
	b := tenantView(st, "beta")
	// 集群每维恰好放满 100 个 (100,100) 任务；3:1 的权重比给出
	// 确定性结果 alpha=75、beta=25（提交阶段先放入 10 个，释放后补齐）。
	if len(a.Running) != 75 || len(b.Running) != 25 {
		t.Fatalf("weighted placement want alpha=75 beta=25, got alpha=%d beta=%d",
			len(a.Running), len(b.Running))
	}
	// 加权份额必须精确相等：75/100/1 == (25/100)/(1/3) == 3/4。
	wa := ratFrom(a.WeightedShare)
	wb := ratFrom(b.WeightedShare)
	if wa.Cmp(wb) != 0 {
		t.Fatalf("weighted shares should be exactly equal, got %s vs %s",
			wa.RatString(), wb.RatString())
	}
	if st.Free.CPU != 0 || st.Free.Mem != 0 {
		t.Fatalf("cluster should be fully packed, got free %+v", st.Free)
	}
	if st.NumQueued != 60 {
		t.Fatalf("want 60 tasks left queued, got %d", st.NumQueued)
	}
	assertConservation(t, s)
}

// 配额是硬上限：租户即使独享集群也不能超过配额；释放不影响配额记账。
func TestDRF_QuotaHardCap(t *testing.T) {
	s := newTestScheduler(t)
	q := Quota{CPU: 4000, Mem: 4000}
	mustTenant(t, s, "limited", nil, &q)

	submit(t, s, "t1", "limited", 3000, 1000)
	submit(t, s, "t2", "limited", 1000, 3000)
	// 此时 (4000,4000) 恰好用完配额；集群其实还有 (6000,6000) 空闲
	st := s.Snapshot()
	tn := tenantView(st, "limited")
	if tn.Allocated != (Resources{CPU: 4000, Mem: 4000}) {
		t.Fatalf("alloc = %+v", tn.Allocated)
	}
	submit(t, s, "t3", "limited", 1, 1) // 再小也会因配额排队
	v, _ := s.TaskSnapshot("t3")
	if v.Status != StatusQueued {
		t.Fatalf("t3 should be queued by quota, got %s", v.Status)
	}
	if v.BlockedReason != ReasonQuotaCPU && v.BlockedReason != ReasonQuotaMem {
		t.Fatalf("t3 blocked reason = %q, want a quota reason", v.BlockedReason)
	}
	if st.Free.CPU != 6000 || st.Free.Mem != 6000 {
		t.Fatalf("cluster should retain free resources, got free %+v", st.Free)
	}
	// 释放 t1，配额空出 3000 CPU；t3 只需 (1,1)，立即运行
	if err := s.ReleaseTask("t1"); err != nil {
		t.Fatal(err)
	}
	if v, _ := s.TaskSnapshot("t3"); v.Status != StatusRunning {
		t.Fatalf("t3 should run after quota freed, got %s", v.Status)
	}
	assertConservation(t, s)
}

// 永远放不下的任务：单任务需求超过集群容量或配额，直接 400 类错误拒收，
// 不进入队列（它只会永远阻塞同租户后续任务）。
func TestDRF_TaskLargerThanCapacityRejected(t *testing.T) {
	s := newTestScheduler(t)
	mustTenant(t, s, "alpha", nil, nil)
	_, err := s.SubmitTask(TaskSpec{ID: "huge", Tenant: "alpha", Demand: Resources{CPU: 20000, Mem: 1}})
	if err == nil {
		t.Fatal("want error for task exceeding cluster capacity")
	}
	if _, ok := s.TaskSnapshot("huge"); ok {
		t.Fatal("rejected task must not exist")
	}

	q := Quota{CPU: 2000, Mem: 10000}
	mustTenant(t, s, "smallq", nil, &q)
	_, err = s.SubmitTask(TaskSpec{ID: "qbig", Tenant: "smallq", Demand: Resources{CPU: 3000, Mem: 1}})
	if err == nil {
		t.Fatal("want error for task exceeding tenant quota")
	}
}

// 队头阻塞：同租户严格 FIFO，后面的小任务不能越过放不进的大任务。
func TestDRF_HeadOfLineBlocking(t *testing.T) {
	s := newTestScheduler(t)
	mustTenant(t, s, "alpha", nil, nil)
	mustTenant(t, s, "beta", nil, nil)

	// beta 占走 9000/9000，留给 alpha 的空间 (1000,1000)
	submit(t, s, "b1", "beta", 9000, 9000)
	submit(t, s, "big", "alpha", 5000, 5000) // 队头，放不下
	submit(t, s, "small", "alpha", 100, 100) // 本放得下，但必须排在 big 后面
	v, _ := s.TaskSnapshot("small")
	if v.Status != StatusQueued || v.BlockedReason != ReasonWaitingInQueue {
		t.Fatalf("small should wait behind head, got %s/%q", v.Status, v.BlockedReason)
	}
	// 释放 beta，big 得以运行；之后 small 仍然排队（big 占 5000，剩余够 small）
	if err := s.ReleaseTask("b1"); err != nil {
		t.Fatal(err)
	}
	if v, _ := s.TaskSnapshot("big"); v.Status != StatusRunning {
		t.Fatalf("big should run, got %s", v.Status)
	}
	if v, _ := s.TaskSnapshot("small"); v.Status != StatusRunning {
		t.Fatalf("small should run right after big (5000+100 fits), got %s", v.Status)
	}
	assertConservation(t, s)
}

// 模糊测试：随机提交/释放，全程断言资源守恒与“不超卖”。
func TestDRF_RandomizedConservation(t *testing.T) {
	s := newTestScheduler(t)
	rng := rand.New(rand.NewSource(42))
	names := []string{"alpha", "beta", "gamma", "delta"}
	for _, n := range names {
		var q *Quota
		if rng.Intn(2) == 0 {
			qq := Quota{CPU: int64(3000 + rng.Intn(7000)), Mem: int64(3000 + rng.Intn(7000))}
			q = &qq
		}
		var w *big.Rat
		if rng.Intn(2) == 0 {
			w = big.NewRat(int64(1+rng.Intn(4)), int64(1+rng.Intn(3)))
		}
		mustTenant(t, s, n, w, q)
	}

	nextSeq := map[string]int{}
	allTasks := []string{}
	for step := 0; step < 2000; step++ {
		if len(allTasks) > 0 && rng.Intn(3) == 0 {
			idx := rng.Intn(len(allTasks))
			id := allTasks[idx]
			allTasks = append(allTasks[:idx], allTasks[idx+1:]...)
			if err := s.ReleaseTask(id); err != nil {
				t.Fatalf("step %d release %s: %v", step, id, err)
			}
		} else {
			tn := names[rng.Intn(len(names))]
			nextSeq[tn]++
			id := tn + "-" + itoa(nextSeq[tn])
			cpu := int64(1 + rng.Intn(4000))
			mem := int64(1 + rng.Intn(4000))
			_, err := s.SubmitTask(TaskSpec{ID: id, Tenant: tn, Demand: Resources{CPU: cpu, Mem: mem}})
			if err == nil {
				allTasks = append(allTasks, id)
			}
			// err 非 nil 仅可能是任务超过容量/配额或重复 ID（id 单调不重复）
		}
		if step%25 == 0 {
			assertConservation(t, s)
		}
	}
	assertConservation(t, s)
}

// 确定性回放：同一串操作反复执行，快照必须逐字节一致（与 map 遍历顺序无关）。
func TestDRF_DeterministicReplay(t *testing.T) {
	ops := func(s *Scheduler) {
		mustTenant(t, s, "zeta", nil, nil)
		mustTenant(t, s, "alpha", ratFrom("1/3"), nil)
		mustTenant(t, s, "mid", ratFrom("0.1"), nil)
		submit(t, s, "z1", "zeta", 2500, 300)
		submit(t, s, "a1", "alpha", 300, 2500)
		submit(t, s, "m1", "mid", 1500, 1500)
		submit(t, s, "z2", "zeta", 2500, 300)
		submit(t, s, "a2", "alpha", 300, 2500)
		submit(t, s, "m2", "mid", 1500, 1500)
		submit(t, s, "z3", "zeta", 2500, 300)
		submit(t, s, "a3", "alpha", 300, 2500)
		_ = s.ReleaseTask("m1")
		submit(t, s, "z4", "zeta", 2500, 300)
	}
	first := func() StateView {
		s := newTestScheduler(t)
		ops(s)
		return s.Snapshot()
	}
	want := first()
	for i := 0; i < 20; i++ {
		s := newTestScheduler(t)
		ops(s)
		got := s.Snapshot()
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("replay %d differs from first run", i+1)
		}
	}
	// 租户必须按 ID 字典序输出
	ids := make([]string, 0, len(want.Tenants))
	for _, tn := range want.Tenants {
		ids = append(ids, tn.ID)
	}
	if !sort.StringsAreSorted(ids) {
		t.Fatalf("tenants not sorted: %v", ids)
	}
}

// 离散任务的公平偏差（indivisibility gap）。
//
// 连续资源 DRF 能做到份额严格相等；不可拆分任务下，每一步放置都是“一整个
// 任务份额”的跳变，因此两个等权租户的份额差不可能为任意小。可证明的上界是：
// 任意时刻，任意两个持续有排队任务的租户，其主导份额之差不超过
// “最大单个任务的主导份额”。下面用 (3000,3000) 的任务（份额 3/10）验证。
func TestDRF_FairnessDeviationBoundedByOneTask(t *testing.T) {
	s := newTestScheduler(t)
	mustTenant(t, s, "filler", nil, nil)
	mustTenant(t, s, "alpha", nil, nil)
	mustTenant(t, s, "beta", nil, nil)

	// filler 占 7000，两个租户各排 4 个 (3000,3000) 任务。
	submit(t, s, "f", "filler", 7000, 7000)
	for i := 0; i < 4; i++ {
		submit(t, s, id("a", i), "alpha", 3000, 3000)
		submit(t, s, id("b", i), "beta", 3000, 3000)
	}

	oneTask := big.NewRat(3, 10)
	// 偏差上界只对“仍有排队需求”的租户之间成立；某个租户队列耗尽后
	// 份额归零是它不再需要资源，不属于公平偏差。
	checkBound := func(stage string) {
		t.Helper()
		st := s.Snapshot()
		av := tenantView(st, "alpha")
		bv := tenantView(st, "beta")
		if len(av.Queued) > 0 && len(bv.Queued) > 0 {
			sa := ratFrom(av.DominantShare)
			sb := ratFrom(bv.DominantShare)
			d := new(big.Rat).Sub(sa, sb)
			if d.Sign() < 0 {
				d.Neg(d)
			}
			if d.Cmp(oneTask) > 0 {
				t.Fatalf("%s: share gap %s between contending tenants exceeds one-task bound 3/10",
					stage, d.RatString())
			}
		}
		assertConservation(t, s)
	}

	// 释放 filler 7000：一次调度中按 alpha,beta,alpha 放置 3 个
	// （平局 ID 偏向 alpha），偏差恰为一个任务份额 0.3。
	if err := s.ReleaseTask("f"); err != nil {
		t.Fatal(err)
	}
	st := s.Snapshot()
	if runningCounts(st)["alpha"] != 2 || runningCounts(st)["beta"] != 1 {
		t.Fatalf("after release: want alpha=2 beta=1, got %v", runningCounts(st))
	}
	d := new(big.Rat).Sub(
		ratFrom(tenantView(st, "alpha").DominantShare),
		ratFrom(tenantView(st, "beta").DominantShare),
	)
	if d.Cmp(oneTask) != 0 {
		t.Fatalf("indivisibility gap want exactly 3/10, got %s", d.RatString())
	}
	checkBound("after-filler-release")

	// 逐个释放运行中的任务直到整个工作负载清空。每一步之后：
	//   1) 份额差始终不超过一个任务份额（3/10）；
	//   2) 资源守恒、不超卖；
	//   3) 释放出的资源立即被排队任务重调度使用。
	const maxIters = 64
	for i := 0; i < maxIters; i++ {
		st0 := s.Snapshot()
		var victim string
		for _, tn := range st0.Tenants {
			if tn.ID == "alpha" || tn.ID == "beta" {
				if len(tn.Running) > 0 {
					victim = tn.Running[0].ID
					break
				}
			}
		}
		if victim == "" {
			if st0.NumQueued != 0 {
				t.Fatalf("no running tasks but %d queued", st0.NumQueued)
			}
			break // 全部完成
		}
		if err := s.ReleaseTask(victim); err != nil {
			t.Fatal(err)
		}
		checkBound("drain-" + itoa(i))
	}

	// 工作负载全部完成后，分配必须回到零且无泄漏。
	st = s.Snapshot()
	if st.NumRunning != 0 || st.NumQueued != 0 {
		t.Fatalf("workload not drained: running=%d queued=%d", st.NumRunning, st.NumQueued)
	}
	if st.Used.CPU != 0 || st.Used.Mem != 0 || st.Free != st.Capacity {
		t.Fatalf("allocation leaked after drain: used=%+v free=%+v", st.Used, st.Free)
	}
}

// 删除一个“卡住的队首”排队任务后，其后更小、本可放置的任务应立即被调度
// （释放排队任务虽不释放资源，但改变了租户队首的可行性）。
func TestDRF_RemovingBlockedHeadSchedulesNext(t *testing.T) {
	s := newTestScheduler(t)
	mustTenant(t, s, "alpha", nil, nil)
	mustTenant(t, s, "beta", nil, nil)

	// beta 占 9000/9000；alpha 队首 big 要 5000 放不下，small 要 500 本可放下
	submit(t, s, "b1", "beta", 9000, 9000)
	submit(t, s, "big", "alpha", 5000, 5000)
	submit(t, s, "small", "alpha", 500, 500)
	if v, _ := s.TaskSnapshot("small"); v.Status != StatusQueued {
		t.Fatalf("small initially queued by HOL blocking, got %s", v.Status)
	}
	// 直接撤销 big（排队任务）：small 成为队首，但集群只有 1000 空闲，放得下
	if err := s.ReleaseTask("big"); err != nil {
		t.Fatal(err)
	}
	if v, _ := s.TaskSnapshot("small"); v.Status != StatusRunning {
		t.Fatalf("small should run after blocked head removed, got %s (reason=%s)",
			v.Status, v.BlockedReason)
	}
	assertConservation(t, s)
}

// 输入校验
func TestDRF_Validation(t *testing.T) {
	if _, err := New(Resources{CPU: 0, Mem: 10}); err == nil {
		t.Fatal("zero capacity should be rejected")
	}
	s := newTestScheduler(t)
	if err := s.CreateTenant(TenantSpec{ID: "x", Weight: big.NewRat(0, 1)}); err == nil {
		t.Fatal("zero weight should be rejected")
	}
	if err := s.CreateTenant(TenantSpec{ID: "x", Weight: big.NewRat(-1, 1)}); err == nil {
		t.Fatal("negative weight should be rejected")
	}
	mustTenant(t, s, "x", nil, nil)
	if err := s.CreateTenant(TenantSpec{ID: "x"}); err == nil {
		t.Fatal("duplicate tenant should be rejected")
	}
	if err := s.DeleteTenant("nope"); err == nil {
		t.Fatal("deleting missing tenant should error")
	}
	submit(t, s, "t1", "x", 1, 1)
	if err := s.DeleteTenant("x"); err == nil {
		t.Fatal("deleting tenant with tasks should error")
	}
	if err := s.ReleaseTask("nope"); err == nil {
		t.Fatal("releasing missing task should error")
	}
	if _, err := s.SubmitTask(TaskSpec{ID: "y", Tenant: "ghost", Demand: Resources{CPU: 1, Mem: 1}}); err == nil {
		t.Fatal("submitting to missing tenant should error")
	}
	if _, err := s.SubmitTask(TaskSpec{ID: "z", Tenant: "x", Demand: Resources{CPU: 0, Mem: 1}}); err == nil {
		t.Fatal("zero demand should be rejected")
	}
}

// ---- 小工具 ----

func tenantView(st StateView, id string) TenantView {
	for _, tn := range st.Tenants {
		if tn.ID == id {
			return tn
		}
	}
	return TenantView{}
}

func runningCounts(st StateView) map[string]int {
	m := map[string]int{}
	for _, tn := range st.Tenants {
		m[tn.ID] = len(tn.Running)
	}
	return m
}

func id(prefix string, i int) string { return prefix + "-" + itoa(i) }

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b strings.Builder
	for i > 0 {
		b.WriteByte(byte('0' + i%10))
		i /= 10
	}
	return b.String()
}
