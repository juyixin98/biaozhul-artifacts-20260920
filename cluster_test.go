package main

import (
	"testing"
	"time"
)

func TestClockCompare(t *testing.T) {
	cases := []struct {
		name string
		a, b Clock
		want int
	}{
		{"equal", Clock{"w1": 1}, Clock{"w1": 1}, 0},
		{"a-before-b", Clock{"w1": 1}, Clock{"w1": 1, "w2": 1}, -1},
		{"a-after-b", Clock{"w1": 2}, Clock{"w1": 1}, 1},
		{"concurrent", Clock{"w1": 1}, Clock{"w2": 1}, 2},
		{"concurrent-forked", Clock{"w1": 1, "w2": 1}, Clock{"w1": 1, "w3": 1}, 2},
		{"equal-multicomponent", Clock{"w1": 1, "w2": 2}, Clock{"w2": 2, "w1": 1}, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := cmpClock(tc.a, tc.b); got != tc.want {
				t.Fatalf("cmpClock(%v,%v)=%d want %d", tc.a, tc.b, got, tc.want)
			}
		})
	}
}

func TestPruneSiblings(t *testing.T) {
	v := func(id string, c Clock) Version {
		return Version{ID: id, Key: "k", Clock: c}
	}
	in := []Version{
		v("a", Clock{"w1": 1}),
		v("b", Clock{"w1": 1, "w2": 1}), // b 支配 a
		v("c", Clock{"w1": 1, "w3": 1}), // c 与 b 并发
	}
	out := pruneSiblings(in)
	if len(out) != 2 {
		t.Fatalf("expected 2 maximal siblings, got %d: %+v", len(out), out)
	}
	ids := map[string]bool{}
	for _, x := range out {
		ids[x.ID] = true
	}
	if !ids["b"] || !ids["c"] {
		t.Fatalf("expected survivors b,c got %v", ids)
	}
}

func TestPruneMergesCommittedFlag(t *testing.T) {
	old := Version{ID: "a", Key: "k", Clock: Clock{"w1": 1}, Committed: true, AckCount: 3}
	dup := Version{ID: "a2", Key: "k", Clock: Clock{"w1": 1}, Committed: false, AckCount: 1}
	out := pruneSiblings([]Version{dup, old})
	if len(out) != 1 {
		t.Fatalf("expected 1, got %d", len(out))
	}
	if !out[0].Committed || out[0].AckCount != 3 {
		t.Fatalf("committed metadata not merged: %+v", out[0])
	}
}

// 健康集群（N=3 W=2 R=2）上的基本读写。
func TestBasicWriteRead(t *testing.T) {
	c, err := NewCluster(3, 2, 2)
	if err != nil {
		t.Fatal(err)
	}
	w := c.Write("k1", "hello", nil, 0, WriteOptions{Timeout: time.Second})
	if !w.Quorum {
		t.Fatalf("write quorum expected: %+v", w)
	}
	if w.Acked != 3 {
		t.Fatalf("expected all 3 acks (fast path), got %d", w.Acked)
	}
	r := c.Read("k1", ReadOptions{Timeout: time.Second})
	if !r.Quorum || r.Conflict || r.Value != "hello" {
		t.Fatalf("unexpected read: %+v", r)
	}
	if r.HasUncommitted {
		t.Fatalf("fully committed write must not show uncommitted")
	}
}

// 因果后继写：带 context 的第二次写应取代第一次，而不是形成冲突。
func TestCausalOverwriteNoConflict(t *testing.T) {
	c, _ := NewCluster(3, 2, 2)
	w1 := c.Write("k", "v1", nil, 0, WriteOptions{Timeout: time.Second})
	ctx := w1.Clock
	w2 := c.Write("k", "v2", ctx, 0, WriteOptions{Timeout: time.Second})
	_ = w2
	r := c.Read("k", ReadOptions{Timeout: time.Second})
	if r.Conflict || r.Value != "v2" {
		t.Fatalf("expected causal overwrite v2, got conflict=%v value=%q versions=%+v",
			r.Conflict, r.Value, r.Versions)
	}
}

// 部分写成功后超时：W=3，一个副本 down、一个 delay 超过超时时间，
// 协调者超时返回失败，但 up 副本上留下 uncommitted 版本。
func TestPartialWriteTimeoutLeavesUncommitted(t *testing.T) {
	c, _ := NewCluster(3, 3, 2)
	if err := c.SetFault(2, modeDown, 0); err != nil {
		t.Fatal(err)
	}
	if err := c.SetFault(1, modeDelay, 400*time.Millisecond); err != nil {
		t.Fatal(err)
	}
	w := c.Write("k", "partial", nil, 0, WriteOptions{Timeout: 100 * time.Millisecond})
	if w.Quorum {
		t.Fatalf("write must time out without quorum, got %+v", w)
	}
	if !w.Timeout {
		t.Fatalf("expected timeout=true, got %+v", w)
	}
	// 此时只有副本 0 立即落地了意向。
	if got := c.replicas[0].get("k"); len(got) != 1 || got[0].Committed {
		t.Fatalf("replica0 should hold one uncommitted intent, got %+v", got)
	}

	// 用 R=1 只从副本 0 读：应读到未确认版本，并被明确标记。
	r := c.Read("k", ReadOptions{R: 1, Targets: []int{0}, Timeout: time.Second, Repair: false})
	if !r.Found || !r.HasUncommitted {
		t.Fatalf("expected uncommitted partial write visible from replica0: %+v", r)
	}
	if r.Value != "partial" {
		t.Fatalf("expected value 'partial', got %q", r.Value)
	}
}

// 读修复：写只落在副本 0,1（用 targets 模拟），随后从 0,2 读，
// 修复后副本 2 应补上该版本。
func TestReadRepair(t *testing.T) {
	c, _ := NewCluster(3, 2, 2)
	w := c.Write("k", "x", nil, 0, WriteOptions{Targets: []int{0, 1}, Timeout: time.Second})
	if !w.Quorum {
		t.Fatalf("write to 2 targets with W=2 should succeed: %+v", w)
	}
	if got := c.replicas[2].get("k"); len(got) != 0 {
		t.Fatalf("replica2 should be empty, got %+v", got)
	}
	r := c.Read("k", ReadOptions{Targets: []int{0, 2}, R: 2, Timeout: time.Second, Repair: true})
	if !r.Quorum {
		t.Fatalf("read should reach quorum: %+v", r)
	}
	found := false
	for _, id := range r.RepairedTo {
		if id == 2 {
			found = true
		}
	}
	if !found {
		t.Fatalf("replica2 should have been repaired, repaired_to=%v", r.RepairedTo)
	}
	if got := c.replicas[2].get("k"); len(got) != 1 || !got[0].Committed {
		t.Fatalf("replica2 should hold committed version after repair, got %+v", got)
	}
}

// 并发写：两个不带因果上下文的写（盲写）各自达到 W=2，向量时钟并发，
// 读仲裁把它们都读出来并报告 conflict，绝不静默选一个、绝不标成线性一致。
func TestConcurrentWritesConflict(t *testing.T) {
	c, _ := NewCluster(5, 2, 3)
	a := c.Write("k", "A", nil, 0, WriteOptions{Targets: []int{0, 1}, Timeout: time.Second})
	b := c.Write("k", "B", nil, 0, WriteOptions{Targets: []int{2, 3}, Timeout: time.Second})
	if !a.Quorum || !b.Quorum {
		t.Fatalf("both writes should reach W=2: a=%+v b=%+v", a, b)
	}
	// 从持有 A、持有 B、空副本的各读一个（R=3），兄弟版本被合并。
	r := c.Read("k", ReadOptions{Targets: []int{0, 2, 4}, R: 3, Timeout: time.Second, Repair: false})
	if !r.Quorum {
		t.Fatalf("expected quorum read: %+v", r)
	}
	if !r.Conflict || len(r.Versions) != 2 {
		t.Fatalf("expected 2 concurrent siblings/conflict, got %+v", r)
	}
	if !concurrent(r.Versions[0].Clock, r.Versions[1].Clock) {
		t.Fatalf("versions must be concurrent: %v vs %v", r.Versions[0].Clock, r.Versions[1].Clock)
	}
}

// 即使 W+R>N（3+3>5），并发盲写仍会冲突——证明该不等式不自动保证语义。
func TestConcurrentWritesConflictEvenWithWRGreaterThanN(t *testing.T) {
	c, _ := NewCluster(5, 3, 3)
	// 两次写各自只打到不相交的 3 元集合中的交集方式：0,1,2 与 0,3,4。
	// 两者都达到 W=3，且版本在副本 0 上成为兄弟（并发保留）。
	a := c.Write("k", "A", nil, 0, WriteOptions{Targets: []int{0, 1, 2}, Timeout: time.Second})
	b := c.Write("k", "B", nil, 0, WriteOptions{Targets: []int{0, 3, 4}, Timeout: time.Second})
	if !a.Quorum || !b.Quorum {
		t.Fatalf("both writes should succeed: a=%+v b=%+v", a, b)
	}
	r := c.Read("k", ReadOptions{Timeout: time.Second, Repair: false})
	if !r.Conflict || len(r.Versions) != 2 {
		t.Fatalf("W+R>N but concurrent blind writes must still conflict: %+v", r)
	}
}

// 冲突解决：以两个兄弟版本为共同因果后继写一次，冲突消失。
func TestResolveConflict(t *testing.T) {
	c, _ := NewCluster(3, 2, 2)
	a := c.Write("k", "A", nil, 0, WriteOptions{Targets: []int{0, 1}, Timeout: time.Second})
	b := c.Write("k", "B", nil, 0, WriteOptions{Targets: []int{1, 2}, Timeout: time.Second})
	_ = a
	_ = b
	cur := c.Read("k", ReadOptions{Timeout: time.Second})
	if !cur.Conflict {
		t.Fatalf("precondition: conflict expected")
	}
	ctx := Clock{}
	for _, v := range cur.Versions {
		ctx.mergeInto(v.Clock)
	}
	m := c.Write("k", "merged", ctx, 0, WriteOptions{Timeout: time.Second})
	if !m.Quorum {
		t.Fatalf("merge write should succeed: %+v", m)
	}
	after := c.Read("k", ReadOptions{Timeout: time.Second, Repair: true})
	if after.Conflict || after.Value != "merged" {
		t.Fatalf("conflict not resolved: conflict=%v value=%q versions=%+v",
			after.Conflict, after.Value, after.Versions)
	}
}

// 副本恢复：down 期间错过写入，recover 后通过反熵补齐并保留冲突。
func TestReplicaRecovery(t *testing.T) {
	c, _ := NewCluster(3, 2, 2)
	if err := c.SetFault(2, modeDown, 0); err != nil {
		t.Fatal(err)
	}
	c.Write("k", "v1", nil, 0, WriteOptions{Targets: []int{0, 1}, Timeout: time.Second})
	if got := c.replicas[2].get("k"); len(got) != 0 {
		t.Fatalf("down replica must not receive writes, got %+v", got)
	}
	rec := c.Recover(RecoverOptions{Target: 2})
	if rec.Status != "recovered" {
		t.Fatalf("recover failed: %+v", rec)
	}
	if rec.KeysPulled["k"] != 1 {
		t.Fatalf("expected 1 version pulled for k, got %+v", rec.KeysPulled)
	}
	if got := c.replicas[2].get("k"); len(got) != 1 || got[0].Value != "v1" {
		t.Fatalf("recovered replica missing data: %+v", got)
	}
	if c.replicas[2].status().Mode != "up" {
		t.Fatalf("replica should be up after recover")
	}
	// 恢复后可以正常服务写。
	w := c.Write("k2", "z", nil, 0, WriteOptions{Timeout: time.Second})
	if !w.Quorum || w.Acked != 3 {
		t.Fatalf("post-recovery write should reach all replicas: %+v", w)
	}
}

// 读仲裁不足返回 quorum=false，但 body 仍含部分数据（接口层据此回 503）。
func TestReadWithoutQuorumReturnsPartial(t *testing.T) {
	c, _ := NewCluster(3, 3, 3)
	for _, id := range []int{1, 2} {
		if err := c.SetFault(id, modeDown, 0); err != nil {
			t.Fatal(err)
		}
	}
	c.replicas[0].apply(Version{ID: "x", Key: "k", Value: "solo", Clock: Clock{"w1": 1}})
	r := c.Read("k", ReadOptions{Timeout: 200 * time.Millisecond})
	if r.Quorum {
		t.Fatalf("must not report quorum with only 1 of R=3 responses")
	}
	if !r.Found || r.Value != "solo" {
		t.Fatalf("partial data should still be surfaced in the outcome: %+v", r)
	}
}

func TestSetConfigValidation(t *testing.T) {
	c, _ := NewCluster(3, 2, 2)
	if _, err := c.SetConfig(3, 0, 2); err == nil {
		t.Fatal("W=0 must be rejected")
	}
	if _, err := c.SetConfig(3, 2, 4); err == nil {
		t.Fatal("R>N must be rejected")
	}
	reset, err := c.SetConfig(5, 3, 3)
	if err != nil || !reset {
		t.Fatalf("changing N should reset: err=%v reset=%v", err, reset)
	}
	if n, _, _ := c.Config(); n != 5 {
		t.Fatalf("expected N=5")
	}
}
