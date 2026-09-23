package runner_test

import (
	"os"
	"path/filepath"
	"testing"

	"twopcsim/internal/runner"
)

func cleanNet() runner.NetworkCfg {
	return runner.NetworkCfg{MinDelay: 1, MaxDelay: 3}
}

func base(t *testing.T, name string) runner.Scenario {
	t.Helper()
	dir := t.TempDir()
	return runner.Scenario{
		Name:         name,
		Seed:         1,
		MaxTick:      300,
		DataDir:      filepath.Join(dir, "data"),
		Fresh:        boolPtr(true),
		Network:      cleanNet(),
		Timings:      runner.TimingsForTest(),
		Transactions: []runner.TxnSpec{{ID: "txn-A", BeginTick: 5}},
	}
}

func boolPtr(b bool) *bool { return &b }

func run(t *testing.T, sc runner.Scenario) *runner.Report {
	t.Helper()
	r, err := runner.New(sc)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	r.Run()
	rep := r.Report()
	r.Close()
	return rep
}

// 1) 干净网络下全员提交。
func TestHappyPathCommitsAllThree(t *testing.T) {
	rep := run(t, base(t, "happy"))
	if rep.Verdict != "all-consistent" {
		t.Fatalf("verdict=%s", rep.Verdict)
	}
	o := rep.Outcomes[0]
	if o.Verdict != "committed" || len(o.Committed) != 3 {
		t.Fatalf("应全员提交: %+v", o)
	}
}

// 2) 确定性：同种子两次运行 trace 完全一致。
func TestDeterministic(t *testing.T) {
	mk := func() runner.Scenario {
		sc := base(t, "det")
		sc.Network = runner.NetworkCfg{
			LossRate: 0.2, DuplicateRate: 0.15, ReorderRate: 0.2,
			MinDelay: 1, MaxDelay: 4, ReorderDelay: 7,
		}
		return sc
	}
	a := run(t, mk())
	b := run(t, mk())
	if len(a.Trace) != len(b.Trace) {
		t.Fatalf("trace 长度不同: %d vs %d", len(a.Trace), len(b.Trace))
	}
	for i := range a.Trace {
		x, y := a.Trace[i], b.Trace[i]
		if x.Tick != y.Tick || x.Node != y.Node || x.Kind != y.Kind {
			t.Fatalf("trace 在 #%d 分歧: %+v vs %+v", i, x, y)
		}
	}
}

// 3) 有损网络下仍能达成一致（重发机制）。
func TestLossyNetworkConsensus(t *testing.T) {
	sc := base(t, "lossy")
	sc.Network = runner.NetworkCfg{
		LossRate: 0.3, DuplicateRate: 0.15, ReorderRate: 0.2,
		MinDelay: 1, MaxDelay: 4, ReorderDelay: 8,
	}
	rep := run(t, sc)
	o := rep.Outcomes[0]
	if o.Verdict != "committed" || len(o.Committed) != 3 {
		t.Fatalf("有损网络下应靠重发全员提交: verdict=%s committed=%v", o.Verdict, o.Committed)
	}
}

// 4) 一张否决票 => 全员中止，无部分提交。
func TestVoteNoAbortsAll(t *testing.T) {
	sc := base(t, "voteno")
	sc.Transactions[0].VoteNo = []string{"participant-2"}
	rep := run(t, sc)
	o := rep.Outcomes[0]
	if o.Verdict != "aborted" || len(o.Aborted) != 3 || len(o.Committed) != 0 {
		t.Fatalf("应全员中止: %+v", o)
	}
}

// 5..N) 每个协议阶段崩溃重启 => 无部分提交。
func TestCrashMatrixNoPartialCommit(t *testing.T) {
	type tc struct {
		name        string
		node        string
		hook        string
		restart     int64
		wantVerdict string
	}
	cases := []tc{
		{"coord-start", "coordinator", "coordinator.start.appended", 80, "committed"},
		{"coord-commit", "coordinator", "coordinator.commit.appended", 120, "committed"},
		{"coord-abort", "coordinator", "coordinator.abort.appended", 100, "aborted"},
		{"part-after-prepared", "participant-2", "participant.after.prepared", 40, "committed"},
		{"part-after-commit", "participant-3", "participant.after.commit", 140, "committed"},
		{"part-after-abort", "participant-1", "participant.after.abort", 100, "aborted"},
	}
	// coord-abort / part-after-abort 需要一个投否决的事务。
	for _, c := range cases {
		c := c
		t.Run(c.name, func(t *testing.T) {
			sc := base(t, "crash-"+c.name)
			if c.wantVerdict == "aborted" {
				sc.Transactions[0].VoteNo = []string{"participant-2"}
			}
			sc.Crashes = []runner.CrashRule{{
				NodeID: c.node, Hook: c.hook, RestartTick: c.restart,
			}}
			rep := run(t, sc)
			if rep.Verdict != "all-consistent" {
				t.Fatalf("检测到部分提交/违规: %s %v", rep.Verdict, rep.PartialCommits)
			}
			o := rep.Outcomes[0]
			if o.PartialCommit {
				t.Fatalf("不允许部分提交: %+v", o)
			}
			if o.Verdict != c.wantVerdict {
				t.Fatalf("verdict=%s, 期望 %s", o.Verdict, c.wantVerdict)
			}
			// 崩溃与重启都必须真正发生（验证注入有效）。
			var sum *runner.CrashSummary
			for i := range rep.Crashes {
				if rep.Crashes[i].NodeID == c.node {
					sum = &rep.Crashes[i]
				}
			}
			if sum == nil || !sum.Crashed || !sum.Restarted {
				t.Fatalf("崩溃/重启未按计划发生: %+v", sum)
			}
		})
	}
}

// 6) 协调者提交点后永久不可用 => 参与者阻塞，且绝不自行回滚。
func TestBlockingWhenCoordinatorDownForever(t *testing.T) {
	sc := base(t, "blocked-commit")
	sc.Crashes = []runner.CrashRule{{NodeID: "coordinator", Hook: "coordinator.commit.appended"}}
	rep := run(t, sc)
	o := rep.Outcomes[0]
	if o.Verdict != "blocked" {
		t.Fatalf("应为 blocked，实际 %s", o.Verdict)
	}
	if len(o.Prepared) != 3 {
		t.Fatalf("3 个参与者都应停在 prepared: %v", o.Prepared)
	}
	be := o.BlockedEvidence
	if be == nil || be.CoordinatorAvailable || be.CoordinatorDecision != "commit" {
		t.Fatalf("阻塞证据错误: %+v", be)
	}
	for _, p := range be.Participants {
		if !p.LockHeld || p.WaitedTicks <= 0 || p.QueriesSent < 2 {
			t.Fatalf("参与者 %s 应持锁并多次询问: %+v", p.NodeID, p)
		}
	}
	// 关键：trace 中参与者不得出现任何自行 abort 的动作。
	for _, e := range rep.Trace {
		if len(e.Node) >= 11 && e.Node[:11] == "participant" {
			if e.Kind == "participant.abort.fromPeer" {
				t.Fatalf("参与者不得在 PREPARED 后自行中止: tick=%d %s", e.Tick, e.Node)
			}
		}
	}
}

// 7) 协调者决议前永久不可用 => 经典 2PC 阻塞（协调者磁盘无决议）。
func TestBlockingBeforeDecision(t *testing.T) {
	sc := base(t, "blocked-predecision")
	sc.Crashes = []runner.CrashRule{{NodeID: "coordinator", AtTick: 9}}
	rep := run(t, sc)
	o := rep.Outcomes[0]
	if o.Verdict != "blocked" || len(o.Prepared) != 3 {
		t.Fatalf("应全员阻塞: verdict=%s prepared=%v", o.Verdict, o.Prepared)
	}
	if o.BlockedEvidence.CoordinatorDecision != "unknown" {
		t.Fatalf("决议前崩溃磁盘上不应有决议: %s", o.BlockedEvidence.CoordinatorDecision)
	}
}

// 8) 跨进程恢复：第一次运行协调者保持宕机；第二次进程用同一数据目录 resume，
// 协调者恢复后事务必须收敛到提交（证明阻塞是“等待协调者”而非数据丢失）。
func TestCrossProcessResumeCommits(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "shared-data")

	first := base(t, "resume-1")
	first.DataDir = dir
	first.Crashes = []runner.CrashRule{{NodeID: "coordinator", Hook: "coordinator.commit.appended"}}
	r1 := run(t, first)
	if r1.Outcomes[0].Verdict != "blocked" {
		t.Fatalf("第一次运行应阻塞，实际 %s", r1.Outcomes[0].Verdict)
	}
	if entries, err := os.ReadDir(dir); err != nil || len(entries) != 4 {
		t.Fatalf("应有 4 个 WAL 文件, err=%v entries=%v", err, entries)
	}

	// 第二次进程：同一目录、不 Begin 新事务、不注入崩溃、不清盘。
	second := runner.Scenario{
		Name:    "resume-2",
		Seed:    1,
		MaxTick: 300,
		DataDir: dir,
		Fresh:   boolPtr(false),
		Network: cleanNet(),
		Timings: runner.TimingsForTest(),
	}
	r2 := run(t, second)
	o := r2.Outcomes[0]
	if o.Verdict != "committed" || len(o.Committed) != 3 {
		t.Fatalf("恢复后应全员提交: verdict=%s committed=%v", o.Verdict, o.Committed)
	}
	if o.PartialCommit {
		t.Fatal("恢复后出现部分提交")
	}
}

// 9) 崩溃后磁盘状态保持：PREPARED 已 fsync 的参与者即使宕机，
// 报告也应能从 WAL 重建出 prepared（持锁）状态。
func TestDurableStateReconstructedFromWAL(t *testing.T) {
	sc := base(t, "wal-durable")
	sc.MaxTick = 200
	// 参与者写完 PREPARED、投完票后崩溃，且不重启。
	sc.Crashes = []runner.CrashRule{{NodeID: "participant-2", Hook: "participant.after.prepared"}}
	rep := run(t, sc)
	for _, snap := range rep.NodeSnapshots {
		if snap.ID == "participant-2" {
			v, ok := snap.TxnStates["txn-A"]
			if !ok || v.State != "prepared" || !v.LockHeld {
				t.Fatalf("磁盘重建状态错误: %+v ok=%v", v, ok)
			}
			return
		}
	}
	t.Fatal("缺少 participant-2 快照")
}
