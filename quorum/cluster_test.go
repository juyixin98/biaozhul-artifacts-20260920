package quorum

import (
	"testing"
	"time"
)

// fastTiming 让测试快速但仍保留“延迟后才落地”的真实部分写语义。
func fastTiming() Option {
	return WithTiming(2*time.Millisecond, 8*time.Millisecond)
}

func newTestCluster(t *testing.T, cfg Config) *Cluster {
	t.Helper()
	c, err := New(cfg, fastTiming())
	if err != nil {
		t.Fatalf("建集群失败: %v", err)
	}
	return c
}

func mustRead(t *testing.T, c *Cluster, req ReadRequest) ReadResponse {
	t.Helper()
	r, err := c.Read(req)
	if err != nil {
		t.Fatalf("读失败: %v", err)
	}
	return r
}

func TestBasicWriteRead(t *testing.T) {
	c := newTestCluster(t, Config{N: 3, W: 2, R: 2})

	w, err := c.Write(WriteRequest{Key: "k", Value: "v1", Coordinator: "n1"})
	if err != nil {
		t.Fatal(err)
	}
	// 写扇出到全部 N 个副本，达到 W=2 即成功返回；在线副本最终全部确认。
	if !w.QuorumMet || len(w.AckedBy) != 3 {
		t.Fatalf("W=2 成功且 3 个在线副本最终都应确认: %+v", w)
	}
	if got := w.Version.Clock.String(); got != "{n1:1}" {
		t.Fatalf("首写钟应为 {n1:1}, got %s", got)
	}

	r := mustRead(t, c, ReadRequest{Key: "k"})
	if !r.QuorumMet || r.NotFound || r.Conflict {
		t.Fatalf("应读到唯一已提交版本: %+v", r)
	}
	if len(r.Versions) != 1 || r.Versions[0].Value != "v1" {
		t.Fatalf("读到内容错误: %+v", r.Versions)
	}
}

// 场景一：部分写成功后超时。
// 3 副本宕 2，W=2 的写只有 1 个副本落地 -> 整体超时（504 语义），
// 但该写不回滚；恢复后读修复追平其余副本。
func TestPartialWriteTimeoutThenReadRepair(t *testing.T) {
	c := newTestCluster(t, Config{N: 3, W: 2, R: 2})
	if err := c.SetDown("n2"); err != nil {
		t.Fatal(err)
	}
	if err := c.SetDown("n3"); err != nil {
		t.Fatal(err)
	}

	w, err := c.Write(WriteRequest{Key: "k", Value: "v1", Coordinator: "n1"})
	if err != nil {
		t.Fatal(err)
	}
	if w.QuorumMet {
		t.Fatalf("只有 1 个在线副本，W=2 不应达成: %+v", w)
	}
	if len(w.AckedBy) != 1 || w.AckedBy[0] != "n1" {
		t.Fatalf("仅 n1 应落地部分写, got %v", w.AckedBy)
	}

	// 部分写不回滚：n1 上确实有数据。
	if vs := c.replica["n1"].versions("k"); len(vs) != 1 || vs[0].Value != "v1" {
		t.Fatalf("n1 应保留部分写, got %+v", vs)
	}

	// 恢复 n2/n3，它们带着空（旧）数据回来。
	if err := c.SetUp("n2"); err != nil {
		t.Fatal(err)
	}
	if err := c.SetUp("n3"); err != nil {
		t.Fatal(err)
	}
	st := c.State("k")
	if len(st.Replicas["n2"]["k"]) != 0 || len(st.Replicas["n3"]["k"]) != 0 {
		t.Fatalf("恢复瞬间 n2/n3 不应有数据: %+v", st.Replicas)
	}

	// R=2 读：n1 的值进入读集合，读出 v1 并把 n2/n3 修复。
	r := mustRead(t, c, ReadRequest{Key: "k"})
	if !r.QuorumMet || r.Conflict || r.NotFound {
		t.Fatalf("法定人数读应得到唯一版本: %+v", r)
	}
	if r.Versions[0].Value != "v1" {
		t.Fatalf("读到 %s", r.Versions[0].Value)
	}
	repaired := map[string]bool{}
	for _, id := range r.RepairedTo {
		repaired[id] = true
	}
	if !repaired["n2"] || !repaired["n3"] {
		t.Fatalf("读修复应覆盖 n2/n3, got %v", r.RepairedTo)
	}
	for _, id := range []string{"n2", "n3"} {
		if vs := c.replica[id].versions("k"); len(vs) != 1 || vs[0].ID != w.Version.ID {
			t.Fatalf("%s 修复后应持有同一版本: %+v", id, vs)
		}
	}
}

// 场景二：并发写产生兄弟版本，读暴露冲突，且该历史不得被称为线性一致。
func TestConcurrentWritesConflictAndResolve(t *testing.T) {
	c := newTestCluster(t, Config{N: 3, W: 2, R: 2})

	// 两个无因果关系的写，分别只接触互不相同的 2 副本集合：
	// A -> {n1,n2}, B -> {n2,n3}；n2 同时持有二者。
	wa, err := c.Write(WriteRequest{Key: "k", Value: "A", Coordinator: "n1", Nodes: []string{"n1", "n2"}})
	if err != nil {
		t.Fatal(err)
	}
	wb, err := c.Write(WriteRequest{Key: "k", Value: "B", Coordinator: "n2", Nodes: []string{"n2", "n3"}})
	if err != nil {
		t.Fatal(err)
	}
	if !ClockConcurrent(wa.Version.Clock, wb.Version.Clock) {
		t.Fatalf("两次无因果写的钟必须不可比较: %s vs %s", wa.Version.Clock, wb.Version.Clock)
	}

	r := mustRead(t, c, ReadRequest{Key: "k"})
	if !r.QuorumMet {
		t.Fatalf("读应达到 R: %+v", r)
	}
	if !r.Conflict || len(r.Versions) != 2 {
		t.Fatalf("应读到 2 个并发兄弟版本，得到 %+v", r.Versions)
	}
	values := map[string]bool{}
	for _, v := range r.Versions {
		values[v.Value] = true
	}
	if !values["A"] || !values["B"] {
		t.Fatalf("兄弟值应为 A/B: %v", values)
	}

	// 客户端裁决：以两个兄弟钟为父合并后写后继版本。
	res, err := c.Resolve(ResolveRequest{
		Key:         "k",
		Value:       "AB-merged",
		Coordinator: "n3",
		Clocks:      []Clock{wa.Version.Clock, wb.Version.Clock},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.QuorumMet {
		t.Fatalf("消解写应达成 W: %+v", res)
	}
	r2 := mustRead(t, c, ReadRequest{Key: "k"})
	if r2.Conflict || len(r2.Versions) != 1 || r2.Versions[0].Value != "AB-merged" {
		t.Fatalf("消解后应只剩唯一后继版本: %+v", r2.Versions)
	}
	// 后继版本必须在因果上后于两个兄弟版本。
	for _, sib := range []Clock{wa.Version.Clock, wb.Version.Clock} {
		if !ClockLess(sib, res.Version.Clock) {
			t.Fatalf("消解版本应支配兄弟版本: %s !-> %s", sib, res.Version.Clock)
		}
	}
}

// 场景三：副本宕机错过写，恢复后持旧数据，反熵修复追平。
func TestReplicaRecoveryAndAntiEntropy(t *testing.T) {
	c := newTestCluster(t, Config{N: 3, W: 2, R: 2})
	if err := c.SetDown("n3"); err != nil {
		t.Fatal(err)
	}
	w, err := c.Write(WriteRequest{Key: "k", Value: "v1", Coordinator: "n1"})
	if err != nil {
		t.Fatal(err)
	}
	if !w.QuorumMet {
		t.Fatalf("n1/n2 在线，W=2 应达成: %+v", w)
	}

	// n3 宕机期间读不修复它。
	r := mustRead(t, c, ReadRequest{Key: "k"})
	if !r.QuorumMet || len(r.RepairedTo) != 0 {
		t.Fatalf("宕机副本不应被修复: %+v", r)
	}

	if err := c.SetUp("n3"); err != nil {
		t.Fatal(err)
	}
	if vs := c.replica["n3"].versions("k"); len(vs) != 0 {
		t.Fatalf("刚恢复的 n3 应持空（旧）数据: %+v", vs)
	}

	rep, err := c.AntiEntropy(RepairRequest{Key: "k"})
	if err != nil {
		t.Fatal(err)
	}
	updated := map[string]bool{}
	for _, id := range rep.Updated {
		updated[id] = true
	}
	if !updated["n3"] {
		t.Fatalf("反熵应更新 n3: %+v", rep)
	}
	if vs := c.replica["n3"].versions("k"); len(vs) != 1 || vs[0].ID != w.Version.ID {
		t.Fatalf("n3 反熵后应持有 v1 同一版本: %+v", vs)
	}

	// 再跑一次反熵：没有落后者，updated 应为空（幂等）。
	rep2, err := c.AntiEntropy(RepairRequest{Key: "k"})
	if err != nil {
		t.Fatal(err)
	}
	if len(rep2.Updated) != 0 {
		t.Fatalf("反熵应收敛幂等, got %v", rep2.Updated)
	}
}

// 语义对照：W+R<=N 时，读写法定人数可能不相交，即使无并发写也会读到旧值。
func TestWeakQuorumCanReadStale(t *testing.T) {
	c := newTestCluster(t, Config{N: 3, W: 1, R: 1})

	// W=1：写只落到 n1 即成功。
	w, err := c.Write(WriteRequest{Key: "k", Value: "only-on-n1", Coordinator: "n1", Nodes: []string{"n1"}})
	if err != nil {
		t.Fatal(err)
	}
	if !w.QuorumMet {
		t.Fatalf("W=1 单副本即成功: %+v", w)
	}

	// R=1 读一个与写集合不相交的副本 n2 -> key 不存在（旧读）。
	r := mustRead(t, c, ReadRequest{Key: "k", Nodes: []string{"n2"}, NoRepair: true})
	if !r.QuorumMet {
		t.Fatalf("R=1 单副本应算达成: %+v", r)
	}
	if !r.NotFound {
		t.Fatalf("不相交读法定人数下应读不到新值（这不是线性一致）: %+v", r)
	}

	// 换到包含 n1 的读集合即可读到——值一直在，只是弱法定人数不保证交集。
	r2 := mustRead(t, c, ReadRequest{Key: "k", Nodes: []string{"n1"}, NoRepair: true})
	if r2.NotFound || r2.Versions[0].Value != "only-on-n1" {
		t.Fatalf("读集合含 n1 时应读到新值: %+v", r2)
	}
}

// W+R>N 也不自动消解并发冲突：N=3,W=2,R=2 下并发写仍然返回兄弟版本。
// 这与 TestConcurrentWritesConflictAndResolve 构成同一结论的正反两面。
func TestStrongOverlapStillAllowsConcurrentConflict(t *testing.T) {
	c := newTestCluster(t, Config{N: 3, W: 2, R: 2}) // W+R=4 > 3
	wa, _ := c.Write(WriteRequest{Key: "k", Value: "A", Coordinator: "n1", Nodes: []string{"n1", "n2"}})
	wb, _ := c.Write(WriteRequest{Key: "k", Value: "B", Coordinator: "n2", Nodes: []string{"n2", "n3"}})
	if !ClockConcurrent(wa.Version.Clock, wb.Version.Clock) {
		t.Fatalf("即便 W+R>N，并发写仍是并发")
	}
	r := mustRead(t, c, ReadRequest{Key: "k"})
	if !r.Conflict {
		t.Fatalf("W+R>N 不消除冲突：必须返回兄弟版本而不是擅自选一个")
	}
}

func TestConfigValidation(t *testing.T) {
	bad := []Config{
		{N: 0, W: 1, R: 1},
		{N: 3, W: 0, R: 2},
		{N: 3, W: 4, R: 2},
		{N: 3, W: 2, R: 0},
		{N: 3, W: 2, R: 4},
	}
	for _, cfg := range bad {
		if _, err := New(cfg); err == nil {
			t.Fatalf("非法配置应报错: %+v", cfg)
		}
	}
	c := newTestCluster(t, Config{N: 3, W: 2, R: 2})
	if err := c.Reconfigure(0, 2); err == nil {
		t.Fatalf("W=0 应被拒绝")
	}
	if err := c.Reconfigure(1, 1); err != nil {
		t.Fatalf("W=1,R=1 应允许: %v", err)
	}
}
