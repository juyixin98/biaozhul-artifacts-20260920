package main

import (
	"fmt"
	"reflect"
	"sync"
	"testing"
)

// 1. Add 必须生成全局唯一标签。
func TestAddGeneratesUniqueTags(t *testing.T) {
	a, b := NewORSet("A"), NewORSet("B")
	seen := map[Tag]bool{}
	for i := 0; i < 100; i++ {
		for _, s := range []*ORSet{a, b} {
			tag := s.Add("x")
			if seen[tag] {
				t.Fatalf("duplicate tag generated: %s", tag)
			}
			seen[tag] = true
		}
	}
}

//  2. 删除只移除「已观察」标签：A 删除 x 时未见到 B 并发的 add，
//     合并后 x 必须仍然存活（add-wins 语义）。
func TestRemoveOnlyObservedTags(t *testing.T) {
	a, b := NewORSet("A"), NewORSet("B")

	a.Add("x")
	// B 同步到 A 的状态后也加了 x（B 的 add 带自己的唯一标签）
	b.Merge(a.Snapshot())
	b.Add("x")

	// A 删除 x：只移除 A 观察到的那个标签
	a.Remove("x")
	if a.Contains("x") {
		t.Fatal("A should not contain x after local remove")
	}

	// 双向合并后，B 并发 add 的标签未被 A 观察到，x 必须复活
	merged := NewORSet("M")
	merged.Merge(a.Snapshot())
	merged.Merge(b.Snapshot())
	if !merged.Contains("x") {
		t.Fatal("concurrent add by B must survive A's remove (add-wins)")
	}
}

// 3. 并发增加/删除：多 goroutine 同时操作同一副本，结果必须合法。
func TestConcurrentAddRemove(t *testing.T) {
	s := NewORSet("A")
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 200; i++ {
				e := fmt.Sprintf("e%d", (g+i)%10)
				s.Add(e)
				s.Contains(e)
				s.Elements()
				if i%3 == 0 {
					s.Remove(e)
				}
			}
		}(g)
	}
	wg.Wait()
	// 不panic、不data race（配合 go test -race）即通过；
	// 再验证快照-合并后元素集合不变。
	m := NewORSet("M")
	m.Merge(s.Snapshot())
	if !reflect.DeepEqual(s.Elements(), m.Elements()) {
		t.Fatal("snapshot/merge round-trip changed the element set")
	}
}

// 4. 重复同步（幂等）：同一状态合并多次，结果与合并一次完全相同。
func TestDuplicateSyncIdempotent(t *testing.T) {
	src := NewORSet("S")
	src.Add("a")
	src.Add("b")
	src.Remove("a")
	st := src.Snapshot()

	once := NewORSet("once")
	once.Merge(st)

	twice := NewORSet("twice")
	for i := 0; i < 5; i++ {
		twice.Merge(st)
	}

	if !Equal(once.Snapshot(), twice.Snapshot()) {
		t.Fatal("merging the same state repeatedly must be idempotent")
	}
	if !reflect.DeepEqual(twice.Elements(), []string{"b"}) {
		t.Fatalf("unexpected elements: %v", twice.Elements())
	}
}

//  5. 三副本分区合并：A 被分区，B/C 互通；各方并发操作后恢复全网同步，
//     三个副本必须收敛到完全相同的状态。
func TestThreeReplicaPartitionMerge(t *testing.T) {
	a, b, c := NewORSet("A"), NewORSet("B"), NewORSet("C")

	// 初始全量同步
	a.Add("shared")
	syncAll(t, a, b, c)

	// --- 分区：A 离线，B<->C 互通 ---
	a.Add("only-a")    // A 离线期间的本地写
	a.Remove("shared") // A 删除了它观察到的 shared
	b.Add("only-b")
	c.Add("only-c")
	c.Remove("shared") // C 也删除了它观察到的 shared（与 A 观察到的是同一标签）
	b.Merge(c.Snapshot())
	c.Merge(b.Snapshot())

	// --- 分区恢复：全互联同步到不动点 ---
	syncAll(t, a, b, c)
	syncAll(t, a, b, c) // 再跑一轮，确认已到不动点

	want := []string{"only-a", "only-b", "only-c"}
	for name, s := range map[string]*ORSet{"A": a, "B": b, "C": c} {
		got := s.Elements()
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("replica %s = %v, want %v", name, got, want)
		}
	}
	// 状态层面也必须逐字节一致（同样的 adds/rems）
	if !Equal(a.Snapshot(), b.Snapshot()) || !Equal(b.Snapshot(), c.Snapshot()) {
		t.Fatal("replica states diverged after heal")
	}
	// shared 的两个 add 标签都被观察并删除，必须保持删除
	if a.Contains("shared") {
		t.Fatal("shared must stay removed: all its tags were observed and tombstoned")
	}
}

func syncAll(t *testing.T, sets ...*ORSet) {
	t.Helper()
	snaps := make([]State, len(sets))
	for i, s := range sets {
		snaps[i] = s.Snapshot()
	}
	for i, s := range sets {
		for j, st := range snaps {
			if i != j {
				s.Merge(st)
			}
		}
	}
}

//  6. 排列测试：把若干副本各自产生的状态当作「消息」，穷举所有投递顺序，
//     任意排列（含重复投递）都必须收敛到同一最终集合。
func TestPermutationConvergence(t *testing.T) {
	// 构造 5 条「消息」：5 个副本各自独立演化后的状态快照
	messages := make([]State, 5)
	for i := range messages {
		s := NewORSet(fmt.Sprintf("R%d", i))
		s.Add(fmt.Sprintf("item-%d", i))
		if i%2 == 0 {
			s.Add("common")
		}
		messages[i] = s.Snapshot()
	}
	// 再制造一条「删除消息」：R5 先合并 R0 的状态再删除 item-0
	r5 := NewORSet("R5")
	r5.Merge(messages[0])
	r5.Remove("item-0")
	messages = append(messages, r5.Snapshot())

	// 期望结果：全量合并一次
	want := NewORSet("want")
	for _, m := range messages {
		want.Merge(m)
	}
	wantElements := want.Elements()
	wantState := want.Snapshot()

	// 穷举 6! = 720 种投递顺序
	permute(messages, func(order []State) {
		r := NewORSet("P")
		for _, m := range order {
			r.Merge(m)
		}
		if !reflect.DeepEqual(r.Elements(), wantElements) {
			t.Fatalf("permutation diverged: got %v, want %v", r.Elements(), wantElements)
		}
		if !Equal(r.Snapshot(), wantState) {
			t.Fatal("permutation converged on elements but not on full state")
		}
		// 同一条消息重复投递也不能改变结果
		r.Merge(order[0])
		r.Merge(order[len(order)-1])
		if !Equal(r.Snapshot(), wantState) {
			t.Fatal("duplicate delivery broke convergence")
		}
	})
}

func permute(states []State, fn func([]State)) {
	var rec func(int)
	rec = func(i int) {
		if i == len(states) {
			fn(states)
			return
		}
		for j := i; j < len(states); j++ {
			states[i], states[j] = states[j], states[i]
			rec(i + 1)
			states[i], states[j] = states[j], states[i]
		}
	}
	rec(0)
}

//  7. 删除先于增加到达（消息乱序的极端情形）：副本先收到「删除 x 的标签 t」，
//     后收到「add x 的标签 t」，最终结果必须与正常顺序一致 —— x 不存在。
func TestRemoveDeliveredBeforeAdd(t *testing.T) {
	src := NewORSet("S")
	src.Add("x")
	addState := src.Snapshot()
	src.Remove("x")
	removeState := src.Snapshot() // 包含 add 与 tombstone

	// 正常顺序
	normal := NewORSet("N")
	normal.Merge(addState)
	normal.Merge(removeState)

	// 乱序：先收到含墓碑的状态，再收到较旧的 add 状态
	reordered := NewORSet("R")
	reordered.Merge(removeState)
	reordered.Merge(addState)

	if normal.Contains("x") || reordered.Contains("x") {
		t.Fatal("x must be absent regardless of delivery order")
	}
	if !Equal(normal.Snapshot(), reordered.Snapshot()) {
		t.Fatal("states must converge regardless of delivery order")
	}
}
