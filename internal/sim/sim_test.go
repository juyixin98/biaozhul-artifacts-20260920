package sim

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// loadExample 从 examples 目录读取场景（测试工作目录为包目录）。
func loadExample(t *testing.T, name string) *Scenario {
	t.Helper()
	path := filepath.Join("..", "..", "examples", name)
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	var sc Scenario
	if err := json.Unmarshal(data, &sc); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	// 测试隔离：每个测试使用独立临时持久化目录。
	sc.DataDir = filepath.Join(t.TempDir(), "data")
	sc.Trace = false
	return &sc
}

// assertCommittedEqual 断言所有存活节点的已提交前缀逐格一致，
// 且与结果中全局已提交列表一致——这是验收的核心安全性断言。
func assertCommittedEqual(t *testing.T, r *Result) {
	t.Helper()
	if !r.InvariantCheck.Passed {
		t.Fatalf("invariant check failed: %v", r.InvariantCheck.Errors)
	}
	// 至少有一个存活节点。
	anyAlive := false
	for _, a := range r.AliveNodes {
		if a {
			anyAlive = true
		}
	}
	if !anyAlive {
		t.Fatal("no alive nodes at end")
	}
	// 全部存活节点 commitIndex 相同，且等于全局已提交长度。
	var wantCI int = -1
	for id, alive := range r.AliveNodes {
		if !alive {
			continue
		}
		ci := r.NodeCommitIndex[id]
		if wantCI == -1 {
			wantCI = ci
		} else if ci != wantCI {
			t.Fatalf("commitIndex divergence: node %d at %d, expected %d", id, ci, wantCI)
		}
	}
	if len(r.Committed) != wantCI {
		t.Fatalf("global committed length %d != node commitIndex %d", len(r.Committed), wantCI)
	}
}

func proposal(r *Result, client string) ProposalResult {
	for _, p := range r.Proposals {
		if p.Client == client {
			return p
		}
	}
	return ProposalResult{Status: "missing"}
}

// TestBasicReplication 验证正常网络下领导者选举与日志复制。
func TestBasicReplication(t *testing.T) {
	r, err := Run(loadExample(t, "basic-replication.json"))
	if err != nil {
		t.Fatal(err)
	}
	assertCommittedEqual(t, r)
	for _, c := range []string{"c1", "c2", "c4"} {
		if p := proposal(r, c); p.Status != "committed" {
			t.Fatalf("%s: want committed, got %q", c, p.Status)
		}
	}
	if p := proposal(r, "c3-to-follower"); p.Status != "notLeader" {
		t.Fatalf("write to follower: want notLeader, got %q", p.Status)
	}
	if len(r.Committed) != 3 {
		t.Fatalf("want 3 committed entries, got %d", len(r.Committed))
	}
	if len(r.LeaderChanges) != 1 {
		t.Fatalf("want exactly 1 leader in stable network, got %d", len(r.LeaderChanges))
	}
}

// TestOldLeaderRevival 覆盖：旧领导者在网络分区中被隔离、继续接受未提交写入、
// 多数派一侧选出新领导者并提交、分区愈合后旧日志被冲突截断覆盖。
func TestOldLeaderRevival(t *testing.T) {
	r, err := Run(loadExample(t, "old-leader-revival.json"))
	if err != nil {
		t.Fatal(err)
	}
	assertCommittedEqual(t, r)

	// 旧领导者在隔离前的写入已提交。
	if p := proposal(r, "c1-before-partition"); p.Status != "committed" || p.Index != 1 {
		t.Fatalf("c1: %+v", p)
	}
	// 隔离期旧领导者的写入绝不能被提交。
	stale := proposal(r, "c2-on-isolated-old-leader")
	if stale.Status == "committed" {
		t.Fatalf("stale write on isolated old leader must NOT be committed: %+v", stale)
	}
	if stale.Status != "accepted" {
		t.Fatalf("stale write should be locally accepted but uncommitted, got %q", stale.Status)
	}
	// 多数派一侧新领导者的写入全部提交。
	for _, c := range []string{"c3-on-new-leader", "c4-after-heal", "c5-after-heal"} {
		if p := proposal(r, c); p.Status != "committed" {
			t.Fatalf("%s: want committed, got %q", c, p.Status)
		}
	}
	// 最终已提交日志中索引2必须是新领导者的条目，旧领导者的分叉已被覆盖。
	if r.Committed[1].Command != "SET z committed-2" {
		t.Fatalf("index 2 must be new-leader entry after conflict resolution, got %q",
			r.Committed[1].Command)
	}
	staleCmd := "SET y stale-uncommitted"
	for _, e := range r.Committed {
		if e.Command == staleCmd {
			t.Fatalf("stale command must never appear in committed log")
		}
	}
	// 至少发生一次领导者更替：旧领导者 node1 -> 新领导者 node2。
	if len(r.LeaderChanges) < 2 {
		t.Fatalf("want leader change after partition, got %+v", r.LeaderChanges)
	}
}

// TestCrashRestart 覆盖：领导者崩溃后重新选举、追随者重启从磁盘恢复并追平、
// 全集群依次崩溃重启后已提交日志完整保留、可以继续提交新条目。
func TestCrashRestart(t *testing.T) {
	r, err := Run(loadExample(t, "crash-restart.json"))
	if err != nil {
		t.Fatal(err)
	}
	assertCommittedEqual(t, r)

	want := []string{"SET a 1", "SET b 2", "SET c 3", "SET d 4", "SET e 5"}
	if len(r.Committed) != len(want) {
		t.Fatalf("want %d committed entries after full-cluster restart, got %d",
			len(want), len(r.Committed))
	}
	for i, cmd := range want {
		if r.Committed[i].Command != cmd {
			t.Fatalf("committed[%d]: want %q got %q", i, cmd, r.Committed[i].Command)
		}
	}
	if p := proposal(r, "c3-to-follower"); p.Status != "notLeader" {
		t.Fatalf("c3 write to follower node2: want notLeader, got %q", p.Status)
	}
	// 所有节点最终存活且日志长度一致（含已恢复的旧领导者）。
	for id, alive := range r.AliveNodes {
		if !alive {
			t.Fatalf("node %d should be alive at end", id)
		}
		if r.NodeLogLength[id] != 5 {
			t.Fatalf("node %d log length %d, want 5", id, r.NodeLogLength[id])
		}
	}
}

// TestMajorityUnsafeWindow 验证多数派失联期间写入不会被确认提交。
func TestMajorityUnsafeWindow(t *testing.T) {
	r, err := Run(loadExample(t, "majority-outage-window.json"))
	if err != nil {
		t.Fatal(err)
	}
	assertCommittedEqual(t, r)
	if len(r.Committed) != 1 || r.Committed[0].Command != "SET ok 1" {
		t.Fatalf("only pre-outage entry may be committed, got %+v", r.Committed)
	}
	if p := proposal(r, "c2-no-quorum"); p.Status != "accepted" {
		t.Fatalf("write during majority outage must remain accepted (unconfirmed), got %q", p.Status)
	}
	// 失联期旧领导者本地日志可含未提交条目，但已提交索引必须停在1。
	if r.NodeCommitIndex[1] != 1 {
		t.Fatalf("survivor commitIndex must stay 1 without quorum, got %d", r.NodeCommitIndex[1])
	}
}

// TestMajorityRecovery 验证多数派恢复后，失联期挂起的写入可被确认，集群重新可用。
func TestMajorityRecovery(t *testing.T) {
	r, err := Run(loadExample(t, "majority-unavailable.json"))
	if err != nil {
		t.Fatal(err)
	}
	assertCommittedEqual(t, r)
	for _, c := range []string{"c1", "c2-no-quorum", "c3-recovered"} {
		if p := proposal(r, c); p.Status != "committed" {
			t.Fatalf("%s: want committed after recovery, got %q", c, p.Status)
		}
	}
}

// TestDeterminism 验证同一场景与种子重复运行产生逐字节一致的结果。
func TestDeterminism(t *testing.T) {
	run := func() *Result {
		r, err := Run(loadExample(t, "old-leader-revival.json"))
		if err != nil {
			t.Fatal(err)
		}
		return r
	}
	a, _ := json.Marshal(run())
	// 第二次运行使用不同的临时目录但相同种子，结果除磁盘路径外不应有差异
	// （DataDir 不出现在 Result 中）。
	b, _ := json.Marshal(run())
	if string(a) != string(b) {
		t.Fatalf("non-deterministic results for same scenario+seed")
	}
}

// TestFlakyNetworkSafety 在丢包/重复/乱序网络下验证安全性不变量。
func TestFlakyNetworkSafety(t *testing.T) {
	r, err := Run(loadExample(t, "flaky-network.json"))
	if err != nil {
		t.Fatal(err)
	}
	assertCommittedEqual(t, r)
}

// TestNodeRefParsing 验证节点引用支持数字与 "leader"。
func TestNodeRefParsing(t *testing.T) {
	var r NodeRef
	if err := json.Unmarshal([]byte(`2`), &r); err != nil || r.ID != 2 || r.Leader {
		t.Fatalf("numeric ref parse failed: %+v err=%v", r, err)
	}
	r = NodeRef{}
	if err := json.Unmarshal([]byte(`"leader"`), &r); err != nil || !r.Leader {
		t.Fatalf("leader ref parse failed: %+v err=%v", r, err)
	}
	var bad Scenario
	if err := json.Unmarshal([]byte(`{"nodeCount":4}`), &bad); err != nil {
		t.Fatalf("unexpected parse error: %v", err)
	}
	if err := bad.validate(); err == nil {
		t.Fatal("nodeCount 4 should be rejected by validate")
	}
}
