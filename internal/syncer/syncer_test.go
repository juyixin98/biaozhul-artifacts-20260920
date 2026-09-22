package syncer_test

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"nodesync/internal/harness"
	"nodesync/internal/storage"
	"nodesync/internal/syncer"
)

// TestFullSyncWithAllFaults 是主验收场景：
// 中段 payload 损坏、错误父哈希、短段、超时、gRPC 错误、虚高宣称高度同时存在；
// 同步器必须换源重试、不跳过缺口，最终链与可信样例完全一致，并留下来源证据。
func TestFullSyncWithAllFaults(t *testing.T) {
	fx := newFixture(t)
	c := startCluster(t, fx, standardFaults(), []string{"node-a", "node-b", "node-c"})
	st := testStore(t)

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	summary, err := syncer.Run(ctx, baseConfig(c, st))
	if err != nil {
		t.Fatalf("同步应在换源后成功，实际失败: %v", err)
	}
	if !summary.ReachedTarget || summary.VerifiedTip != testTip {
		t.Fatalf("应到达目标 %d，实际 verified_tip=%d reached=%v", testTip, summary.VerifiedTip, summary.ReachedTarget)
	}

	assertFullChain(t, st, fx)

	report, err := syncer.BuildReport(st, fx.TrustedSample, summary)
	if err != nil {
		t.Fatalf("构建报告: %v", err)
	}
	if !report.TipMatchesSample || !report.ContinuousFromZero {
		t.Fatalf("链尖样例一致=%v 连续=%v，均应为 true", report.TipMatchesSample, report.ContinuousFromZero)
	}
	for _, cp := range report.Checkpoints {
		if !cp.Matches {
			t.Fatalf("检查点 %d 与样例不匹配: got=%s want=%s", cp.Height, cp.GotHash, cp.WantHash)
		}
		if cp.SourceNode == "" {
			t.Fatalf("检查点 %d 缺少来源证据", cp.Height)
		}
	}

	// 必须真实经历过失败/拒绝（坏段被换源），而不是一次成功。
	if summary.SegmentsRejected == 0 {
		t.Fatalf("SegmentsRejected=%d，应大于 0（故障被真实触发并拒绝）", summary.SegmentsRejected)
	}
	if len(summary.Warnings) == 0 {
		t.Fatalf("应有虚高宣称高度告警")
	}
	if summary.AdvertisedMax != testTip+10 {
		t.Fatalf("观测到的最大宣称高度应为 %d，实际 %d", testTip+10, summary.AdvertisedMax)
	}

	// 并行上限：任一节点观测到的最大并发在途请求都不得超过 4。
	for _, n := range c.nodes {
		_, maxInflight := n.Stats()
		if maxInflight > 4 {
			t.Fatalf("节点 %s 观测到 %d 并发请求，超过上限 4", n.ID, maxInflight)
		}
	}

	// 证据链完整性。
	ev, err := st.Evidence(0)
	if err != nil {
		t.Fatalf("读取证据: %v", err)
	}
	wantEvents := map[string]bool{
		"peer_tip": false, "segment_rejected": false, "checkpoint_advance": false,
		"advertised_height_probe": false, "sync_complete": false,
	}
	rejectedDetail := strings.Builder{}
	for _, e := range ev {
		if _, ok := wantEvents[e.Event]; ok {
			wantEvents[e.Event] = true
		}
		if e.Event == "segment_rejected" {
			rejectedDetail.WriteString(e.Detail + "\n")
		}
	}
	for evType, seen := range wantEvents {
		if !seen {
			t.Fatalf("证据流中缺少事件类型 %s", evType)
		}
	}
	// 拒绝原因应覆盖全部注入故障的真实错误信息。
	detail := rejectedDetail.String()
	for _, want := range []string{"签名校验失败", "父哈希错误", "短段", "DeadlineExceeded", "Internal"} {
		if !strings.Contains(detail, want) {
			t.Fatalf("拒绝证据中缺少 %q 类故障；实际:\n%s", want, detail)
		}
	}
}

// TestParallelismCap 单独验证"最多 4 段并行"。
func TestParallelismCap(t *testing.T) {
	fx := newFixture(t)
	c := startCluster(t, fx,
		map[string]harness.FaultSpec{
			"node-a": {},
			"node-b": {},
			"node-c": {},
		}, []string{"node-a", "node-b", "node-c"})
	st := testStore(t)
	cfg := baseConfig(c, st)
	cfg.RPCTimeout = 5 * time.Second
	summary, err := syncer.Run(context.Background(), cfg)
	if err != nil {
		t.Fatalf("同步失败: %v", err)
	}
	if summary.VerifiedTip != testTip {
		t.Fatalf("验证高度 %d != %d", summary.VerifiedTip, testTip)
	}
	totalMax := 0
	for _, n := range c.nodes {
		_, mi := n.Stats()
		if mi > 4 {
			t.Fatalf("节点 %s 并发 %d > 4", n.ID, mi)
		}
		totalMax += mi
	}
	// 三节点同时被访问（首轮按段索引轮换），全局峰值可到 4，单节点不会独占 4 槽以上。
	if totalMax < 3 {
		t.Fatalf("并行度证据不足，三节点 max inflight 之和=%d", totalMax)
	}
}

// TestGapCannotBeSkipped：一个高度区间所有源都坏 -> 同步失败，
// 已提交前缀保留，缺口之后的高度一律不得入库。
func TestGapCannotBeSkipped(t *testing.T) {
	fx := newFixture(t)
	// 段 3(24:32) 在三个节点上分别：损坏、错误父哈希、直接错误 —— 全部不可用。
	faults := map[string]harness.FaultSpec{
		"node-a": {CorruptPayloadRanges: []harness.Range{{Start: 24, End: 32}}},
		"node-b": {CorruptParentRanges: []harness.Range{{Start: 24, End: 32}}},
		"node-c": {ErrorRanges: []harness.Range{{Start: 24, End: 32}}},
	}
	c := startCluster(t, fx, faults, []string{"node-a", "node-b", "node-c"})
	st := testStore(t)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	summary, err := syncer.Run(ctx, baseConfig(c, st))
	if !errors.Is(err, syncer.ErrExhausted) {
		t.Fatalf("期望 ErrExhausted，得到 err=%v", err)
	}

	blocks, berr := st.BlocksMap()
	if berr != nil {
		t.Fatal(berr)
	}
	for h := uint64(24); h <= testTip; h++ {
		if _, ok := blocks[h]; ok {
			t.Fatalf("缺口高度 %d 之后的区块不得入库，但高度 %d 存在", 24, h)
		}
	}
	if summary.VerifiedTip != 23 {
		t.Fatalf("检查点应停在 23，实际 %d", summary.VerifiedTip)
	}
}

// TestCancelSafety：取消时在途请求（慢节点）即使返回也不得推进检查点；
// 随后重新运行（模拟恢复）必须能从检查点继续并完成。
func TestCancelSafety(t *testing.T) {
	fx := newFixture(t)
	// node-c 对前三段全部慢响应（远超取消时刻），另外两个节点正常。
	faults := map[string]harness.FaultSpec{
		"node-a": {},
		"node-b": {},
		"node-c": {TimeoutRanges: []harness.Range{{Start: 0, End: 24}}, TimeoutDelay: 3 * time.Second},
	}
	c := startCluster(t, fx, faults, []string{"node-a", "node-b", "node-c"})
	st := testStore(t)
	cfg := baseConfig(c, st)
	cfg.RPCTimeout = 5 * time.Second // 让在途请求跨越取消时刻仍在等待

	// 首轮：在极短时间内取消（在途请求尚未返回）。
	ctx1, cancel1 := context.WithCancel(context.Background())
	done1 := make(chan struct{})
	var s1 *syncer.Summary
	var err1 error
	go func() {
		s1, err1 = syncer.Run(ctx1, cfg)
		close(done1)
	}()
	time.Sleep(80 * time.Millisecond)
	cancel1()
	<-done1

	if err1 != nil && !errors.Is(err1, context.Canceled) {
		// Run 在取消路径上通常返回 nil；若返回错误也必须是取消类。
		t.Fatalf("首轮只允许取消类错误，得到 %v", err1)
	}
	if !s1.Cancelled {
		t.Fatalf("Summary.Cancelled 应为 true")
	}
	tip1, has, err := st.TipHeight()
	if err != nil {
		t.Fatal(err)
	}

	// 等待"取消前已发出的在途旧请求"全部返回（约 3s），期间反复确认检查点不被推进。
	time.Sleep(3200 * time.Millisecond)
	tip2, _, err := st.TipHeight()
	if err != nil {
		t.Fatal(err)
	}
	if has && tip2 != tip1 {
		t.Fatalf("取消后旧请求结果推进了检查点: %d -> %d（严重安全违规）", tip1, tip2)
	}

	// 恢复运行（同一数据库文件）：必须从检查点续传完成。
	ctx2, cancel2 := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel2()
	s2, err2 := syncer.Run(ctx2, cfg)
	if err2 != nil {
		t.Fatalf("恢复同步失败: %v", err2)
	}
	if s2.VerifiedTip != testTip {
		t.Fatalf("恢复后应到 %d，实际 %d", testTip, s2.VerifiedTip)
	}
	assertFullChain(t, st, fx)
}

// TestRestartAcrossStoreReopen：模拟进程重启（关闭并重新打开 SQLite）后续传。
func TestRestartAcrossStoreReopen(t *testing.T) {
	fx := newFixture(t)
	faults := map[string]harness.FaultSpec{
		"node-a": {CorruptPayloadRanges: []harness.Range{{Start: 16, End: 24}}},
		"node-b": {ErrorRanges: []harness.Range{{Start: 40, End: 48}}},
		"node-c": {},
	}
	c := startCluster(t, fx, faults, []string{"node-a", "node-b", "node-c"})

	path := stFilePath(t)
	cfg := baseConfig(c, nil)

	runOnce := func() *syncer.Summary {
		st, err := storage.Open(path)
		if err != nil {
			t.Fatalf("open: %v", err)
		}
		cfg.Store = st
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		s, err := syncer.Run(ctx, cfg)
		if err != nil {
			_ = st.Close()
			t.Fatalf("同步失败: %v", err)
		}
		if err := st.Close(); err != nil {
			t.Fatal(err)
		}
		return s
	}

	// 第一次只同步到高度 31（自定义目标），制造"中途重启"。
	st1, err := storage.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	cfg1 := cfg
	cfg1.Store = st1
	cfg1.TargetTip = 31
	s1, err := syncer.Run(context.Background(), cfg1)
	if err != nil {
		t.Fatalf("第一段同步失败: %v", err)
	}
	if s1.VerifiedTip != 31 {
		t.Fatalf("首段应停在 31，实际 %d", s1.VerifiedTip)
	}
	if err := st1.Close(); err != nil {
		t.Fatal(err)
	}

	// 重新打开（重启），无 TargetTip => 以样例链尖 63 为目标继续。
	s2 := runOnce()
	if s2.VerifiedTip != testTip {
		t.Fatalf("重启后续传应到 %d，实际 %d", testTip, s2.VerifiedTip)
	}
}

// TestTamperedTrustedSampleRejected：可信样例被污染（错误检查点哈希）时必须拒绝，
// 绝不能用远端数据"自证"。
func TestTamperedTrustedSampleRejected(t *testing.T) {
	fx := newFixture(t)
	c := startCluster(t, fx,
		map[string]harness.FaultSpec{"node-a": {}, "node-b": {}, "node-c": {}},
		[]string{"node-a", "node-b", "node-c"})
	st := testStore(t)
	bad := fx.TrustedSample
	bad.Checkpoints[32] = "0000000000000000000000000000000000000000000000000000000000000000"
	cfg := baseConfig(c, st)
	cfg.Sample = bad

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err := syncer.Run(ctx, cfg)
	if !errors.Is(err, syncer.ErrCheckpoint) {
		t.Fatalf("样例哈希被污染时必须返回 ErrCheckpoint，实际 %v", err)
	}
}

// TestBoundedOutOfOrderCache：验证乱序结果缓存有界（<= WindowSegs 段）。
// 让前段（0:8）在所有源上都慢，后段快速返回 -> 后段进入缓存等待缺口；
// 缓存只能容纳 WindowSegs 段，调度窗口不得越过边界。
func TestBoundedOutOfOrderCache(t *testing.T) {
	fx := newFixture(t)
	// 段 0 在 node-a/b 慢（2 次尝试后换到 node-c 成功）；node-c 全部诚实快速。
	// 这样 node-c 对段 1..7 的快速返回会产生乱序缓存。
	faults := map[string]harness.FaultSpec{
		"node-a": {TimeoutRanges: []harness.Range{{Start: 0, End: 8}}, TimeoutDelay: 1 * time.Second},
		"node-b": {TimeoutRanges: []harness.Range{{Start: 0, End: 8}}, TimeoutDelay: 1 * time.Second},
		"node-c": {},
	}
	c := startCluster(t, fx, faults, []string{"node-a", "node-b", "node-c"})
	st := testStore(t)
	cfg := baseConfig(c, st)
	cfg.WindowSegs = 3 // 严格小窗口：最多缓存 3 个乱序段
	cfg.RPCTimeout = 300 * time.Millisecond

	summary, err := syncer.Run(context.Background(), cfg)
	if err != nil {
		t.Fatalf("同步失败: %v", err)
	}
	if summary.VerifiedTip != testTip {
		t.Fatalf("应完整同步到 %d，实际 %d", testTip, summary.VerifiedTip)
	}
	assertFullChain(t, st, fx)

	// 证据中应出现乱序缓存事件，且任何时刻缓存段数不超过窗口。
	ev, _ := st.Evidence(0)
	sawCached := false
	for _, e := range ev {
		if e.Event == "segment_received" && strings.Contains(e.Detail, "乱序缓存") {
			sawCached = true
			// Detail 中包含 "当前缓存 N 段，上限 3"。
			if strings.Contains(e.Detail, "当前缓存 4 段") {
				t.Fatalf("乱序缓存越过有界窗口: %s", e.Detail)
			}
		}
	}
	if !sawCached {
		t.Fatalf("未观察到乱序缓存事件，场景未按预期触发")
	}
}
