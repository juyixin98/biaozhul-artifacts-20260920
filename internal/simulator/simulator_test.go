package simulator_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"snapshotsim/internal/simulator"
)

// totalMoney 返回系统初始总钱数。
func totalMoney(req *simulator.Request) int64 {
	var t int64
	for _, n := range req.Nodes {
		t += n.Balance
	}
	return t
}

// findSnap 从结果中取出指定快照。
func findSnap(t *testing.T, resp *simulator.Response, id string) simulator.SnapshotResult {
	t.Helper()
	for _, s := range resp.Snapshots {
		if s.ID == id {
			return s
		}
	}
	t.Fatalf("快照 %q 不在结果中", id)
	return simulator.SnapshotResult{}
}

// mustComplete 断言快照完整且总量守恒。
func mustComplete(t *testing.T, resp *simulator.Response, id string, wantTotal int64) simulator.SnapshotResult {
	t.Helper()
	s := findSnap(t, resp, id)
	if !s.Complete {
		t.Fatalf("快照 %q 未完成，缺少: %v", id, s.Missing)
	}
	if s.Total != wantTotal {
		t.Fatalf("快照 %q 总量=%d，期望 %d（states=%v, inflight=%v）",
			id, s.Total, wantTotal, s.States, s.InFlight)
	}
	return s
}

// bidirLinks 生成全双向链路（FIFO，默认延迟 1，可指定每对延迟）。
func bidirLinks(nodes []string, baseDelay int) []simulator.LinkSpec {
	var out []simulator.LinkSpec
	for _, a := range nodes {
		for _, b := range nodes {
			if a != b {
				out = append(out, simulator.LinkSpec{From: a, To: b, Policy: simulator.PolicyFIFO, Base: baseDelay})
			}
		}
	}
	return out
}

// ---- 场景 1：空信道（空闲系统上发起快照）----

func TestEmptyChannels(t *testing.T) {
	req := &simulator.Request{
		Seed: 1, TickLimit: 20,
		Nodes: []simulator.NodeSpec{{Name: "A", Balance: 100}, {Name: "B", Balance: 50}},
		Links: bidirLinks([]string{"A", "B"}, 1),
		Events: []simulator.Event{
			{Kind: "marker", Tick: 5, From: "A", SnapshotID: "s0"},
		},
	}
	resp, err := simulator.Execute(req)
	if err != nil {
		t.Fatal(err)
	}
	s := mustComplete(t, resp, "s0", 150)
	if s.States["A"] != 100 || s.States["B"] != 50 {
		t.Fatalf("空信道状态错误: %+v", s.States)
	}
	if len(s.InFlight) != 0 {
		t.Fatalf("空信道不应有在途消息, got %+v", s.InFlight)
	}
	if s.StatesSum != 150 || s.InFlightSum != 0 {
		t.Fatalf("分项求和错误: states=%d inflight=%d", s.StatesSum, s.InFlightSum)
	}
	// 最终余额也守恒
	if resp.FinalBalances["A"]+resp.FinalBalances["B"] != 150 {
		t.Fatalf("最终余额不守恒: %+v", resp.FinalBalances)
	}
}

// ---- 场景 2：在途消息被捕获（“切过信道”的消息必须计入快照）----
//
// 3 个节点、A->B 链路延迟 5（慢链路），其余延迟 1。
// t=1 A 向 B 转 30；t=3 A 发起快照（此时 30 元还在 A->B 慢链路上）。
// 标记经 A->C->B 以 2 tick 抢先到达 B（t=5）：B 记录时余额仍为 50，
// 而转账 t=6 才到、A->B 标记 t=8 才关信道，故 30 元被捕获为在途消息。
// 期望：A=70、B=50、在途 30，总量 200。
func TestInFlightCapture(t *testing.T) {
	links := bidirLinks([]string{"A", "B", "C"}, 1)
	for i := range links {
		if links[i].From == "A" && links[i].To == "B" {
			links[i].Base = 5
		}
	}
	req := &simulator.Request{
		Seed: 1, TickLimit: 30,
		Nodes: []simulator.NodeSpec{
			{Name: "A", Balance: 100}, {Name: "B", Balance: 50}, {Name: "C", Balance: 50},
		},
		Links: links,
		Events: []simulator.Event{
			{Kind: "transfer", Tick: 1, From: "A", To: "B", Amount: 30},
			{Kind: "marker", Tick: 3, From: "A", SnapshotID: "s1"},
		},
	}
	resp, err := simulator.Execute(req)
	if err != nil {
		t.Fatal(err)
	}
	s := mustComplete(t, resp, "s1", 200)
	if s.States["A"] != 70 {
		t.Fatalf("A 记录状态应为 70（已扣款），got %d", s.States["A"])
	}
	if s.States["B"] != 50 {
		t.Fatalf("B 记录状态应为 50（消息未到达），got %d", s.States["B"])
	}
	var found bool
	for _, c := range s.InFlight {
		if c.From == "A" && c.To == "B" {
			found = true
			if c.Count != 1 || c.Sum != 30 {
				t.Fatalf("A->B 应捕获 1 笔 30 元在途消息, got %+v", c)
			}
		}
	}
	if !found {
		t.Fatalf("快照未捕获 A->B 的在途消息: %+v", s.InFlight)
	}
	// 消息最终到达，最终余额 A=70 B=80 C=50
	if resp.FinalBalances["A"] != 70 || resp.FinalBalances["B"] != 80 || resp.FinalBalances["C"] != 50 {
		t.Fatalf("最终余额错误: %+v", resp.FinalBalances)
	}
}

// ---- 场景 3：多个快照 ID 交叠 + 标记交错，互不串台 ----
//
// 全双向 FIFO（延迟 1）。
//
//	t=1 marker s1(A)；t=2 marker s2(B)、并发生一笔 transfer A->B=20。
//
// s1 的全局切点：t=1 起传播。B 在 t=2 收到 s1 标记前，先处理同 tick 的脚本
// （s2 发起 + A->B 转账 enqueue）。脚本先于投递，故这笔转账排在 B 收到的
// s1 标记之后（FIFO），不计入 s1；但它在 B 的 s2 标记（来自 B 自己）……
// 对 s2：B 在记录前已先收到 A->B（该消息 t=3 才投递），故消息在 B 记录 s2
// 之后到达，而 A->B 信道的 s2 标记 t=3 到达，消息 t=3 先于标记（seq 更小），
// 所以计入 s2 的 A->B 在途消息。
func TestOverlappingSnapshotsMarkerInterleave(t *testing.T) {
	req := &simulator.Request{
		Seed: 1, TickLimit: 30,
		Nodes: []simulator.NodeSpec{
			{Name: "A", Balance: 100}, {Name: "B", Balance: 100}, {Name: "C", Balance: 100},
		},
		Links: bidirLinks([]string{"A", "B", "C"}, 1),
		Events: []simulator.Event{
			{Kind: "marker", Tick: 1, From: "A", SnapshotID: "s1"},
			{Kind: "marker", Tick: 2, From: "B", SnapshotID: "s2"},
			{Kind: "transfer", Tick: 2, From: "A", To: "B", Amount: 20},
		},
	}
	resp, err := simulator.Execute(req)
	if err != nil {
		t.Fatal(err)
	}
	const total = int64(300)

	s1 := mustComplete(t, resp, "s1", total)
	if s1.States["A"] != 100 || s1.States["B"] != 100 || s1.States["C"] != 100 {
		t.Fatalf("s1 状态应为全 100, got %+v", s1.States)
	}
	for _, c := range s1.InFlight {
		if c.From == "A" && c.To == "B" {
			t.Fatalf("A->B 的 20 元转账排在 s1 标记之后，不应计入 s1: %+v", c)
		}
	}

	s2 := mustComplete(t, resp, "s2", total)
	if s2.States["A"] != 80 {
		t.Fatalf("s2 中 A 已记录且余额应为 80（t=2 发出 20 后），got %d", s2.States["A"])
	}
	if s2.States["B"] != 100 {
		t.Fatalf("s2 中 B 记录时 20 元尚未到达，应为 100, got %d", s2.States["B"])
	}
	var got int
	for _, c := range s2.InFlight {
		if c.From == "A" && c.To == "B" {
			got = int(c.Sum)
		}
	}
	if got != 20 {
		t.Fatalf("s2 应在 A->B 捕获 20 元在途消息, got %d, all=%+v", got, s2.InFlight)
	}

	// 同一节点不能用相同 id 重复发起（输入校验）
	bad := *req
	bad.Events = append(append([]simulator.Event{}, req.Events...),
		simulator.Event{Kind: "marker", Tick: 3, From: "A", SnapshotID: "s1"})
	if _, err := simulator.Execute(&bad); err == nil {
		t.Fatal("重复发起同一快照应被拒绝")
	}
}

// ---- 场景 4：快照期间持续发送消息（周期转账流中两个交叠快照）----
func TestContinuousTrafficDuringSnapshots(t *testing.T) {
	req := &simulator.Request{
		Seed: 7, TickLimit: 60,
		Nodes: []simulator.NodeSpec{{Name: "A", Balance: 120}, {Name: "B", Balance: 80}},
		Links: bidirLinks([]string{"A", "B"}, 1),
		Events: []simulator.Event{
			// 每 2 tick 双向各一笔 2 元，持续到 t=40
			{Kind: "transfer", Tick: 0, From: "A", To: "B", Amount: 2, RepeatTo: 40, RepeatEvery: 2},
			{Kind: "transfer", Tick: 1, From: "B", To: "A", Amount: 2, RepeatTo: 41, RepeatEvery: 2},
			{Kind: "marker", Tick: 5, From: "A", SnapshotID: "k1"},
			{Kind: "marker", Tick: 12, From: "B", SnapshotID: "k2"},
		},
	}
	resp, err := simulator.Execute(req)
	if err != nil {
		t.Fatal(err)
	}
	const total = int64(200)
	k1 := mustComplete(t, resp, "k1", total)
	k2 := mustComplete(t, resp, "k2", total)

	// 交叠期：k2 发起时 k1 可能尚未完成；二者必须都完成且各自独立
	if k1.Initiator != "A" || k2.Initiator != "B" {
		t.Fatalf("发起者记录错误: k1=%s k2=%s", k1.Initiator, k2.Initiator)
	}
	// 流仍在进行时（t=60，流 t=40/41 结束）所有消息应已投递，最终余额守恒
	if resp.FinalBalances["A"]+resp.FinalBalances["B"] != total {
		t.Fatalf("最终余额不守恒: %+v", resp.FinalBalances)
	}
	// 具体余额：A 发出 t=0,2,...,40 共 21 笔=42；B 发出 t=1,3,...,41 共 21 笔=42
	if resp.FinalBalances["A"] != 120 || resp.FinalBalances["B"] != 80 {
		t.Fatalf("等额双向流后余额应不变, got %+v", resp.FinalBalances)
	}
	// 日志中应能看到持续的 send/deliver 与两个快照各自的 in-flight 记录
	var inflightK1, inflightK2 int
	for _, l := range resp.Log {
		if l.Detail == "recorded in-flight" {
			switch l.SnapID {
			case "k1":
				inflightK1++
			case "k2":
				inflightK2++
			}
		}
	}
	if inflightK1 == 0 && inflightK2 == 0 {
		t.Fatal("持续流期间快照应至少捕获到在途消息")
	}
}

// ---- best_effort 信道：丢包 ----

func TestBestEffortLoss(t *testing.T) {
	req := &simulator.Request{
		Seed: 42, TickLimit: 30,
		Nodes: []simulator.NodeSpec{{Name: "A", Balance: 100}, {Name: "B", Balance: 0}},
		Links: []simulator.LinkSpec{
			{From: "A", To: "B", Policy: simulator.PolicyBestEffort, Base: 2, LossPct: 100},
		},
		Events: []simulator.Event{
			{Kind: "transfer", Tick: 0, From: "A", To: "B", Amount: 10},
			{Kind: "transfer", Tick: 1, From: "A", To: "B", Amount: 10},
		},
	}
	resp, err := simulator.Execute(req)
	if err != nil {
		t.Fatal(err)
	}
	st := resp.ChannelStats[0]
	if st.Sent != 2 || st.Lost != 2 || st.Delivered != 0 {
		t.Fatalf("丢包统计错误: %+v", st)
	}
	if resp.FinalBalances["A"] != 80 || resp.FinalBalances["B"] != 0 {
		t.Fatalf("丢包后发送方扣款、接收方未到账: %+v", resp.FinalBalances)
	}
}

// ---- best_effort 信道：重复投递（接收方会上账两次）----

func TestBestEffortDuplicate(t *testing.T) {
	req := &simulator.Request{
		Seed: 42, TickLimit: 30,
		Nodes: []simulator.NodeSpec{{Name: "A", Balance: 100}, {Name: "B", Balance: 0}},
		Links: []simulator.LinkSpec{
			{From: "A", To: "B", Policy: simulator.PolicyBestEffort, Base: 2, DupPct: 100},
		},
		Events: []simulator.Event{
			{Kind: "transfer", Tick: 0, From: "A", To: "B", Amount: 10},
		},
	}
	resp, err := simulator.Execute(req)
	if err != nil {
		t.Fatal(err)
	}
	st := resp.ChannelStats[0]
	if st.Duplicated != 1 || st.Delivered != 2 {
		t.Fatalf("重复统计错误: %+v", st)
	}
	if resp.FinalBalances["B"] != 20 {
		t.Fatalf("重复消息被上账两次，B 应为 20, got %d", resp.FinalBalances["B"])
	}
}

// ---- best_effort 信道：乱序 + 标记丢失导致快照不完整 ----
//
// 该场景同时演示：Chandy-Lamport 依赖可靠 FIFO；best_effort 信道下
// 快照可能永远完不成（标记丢失），结果以 complete=false 如实报告。
func TestBestEffortReorderAndBrokenSnapshot(t *testing.T) {
	// 在一组确定性 seed 下，reorder=true 时至少出现一次乱序统计
	anyReorder := false
	for seed := int64(0); seed < 200; seed++ {
		req := &simulator.Request{
			Seed: seed, TickLimit: 60,
			Nodes: []simulator.NodeSpec{
				{Name: "A", Balance: 100}, {Name: "B", Balance: 100},
			},
			Links: []simulator.LinkSpec{
				{From: "A", To: "B", Policy: simulator.PolicyBestEffort, Base: 3, Jitter: 3, Reorder: true},
				{From: "B", To: "A", Policy: simulator.PolicyBestEffort, Base: 3, Jitter: 3, Reorder: true},
			},
			Events: []simulator.Event{
				{Kind: "transfer", Tick: 0, From: "A", To: "B", Amount: 1, RepeatTo: 10, RepeatEvery: 1},
				{Kind: "transfer", Tick: 0, From: "B", To: "A", Amount: 1, RepeatTo: 10, RepeatEvery: 1},
			},
		}
		resp, err := simulator.Execute(req)
		if err != nil {
			t.Fatal(err)
		}
		var reordered int
		for _, st := range resp.ChannelStats {
			reordered += st.Reordered
		}
		if reordered > 0 {
			anyReorder = true
			break
		}
	}
	if !anyReorder {
		t.Fatal("200 个确定性 seed 中未观察到乱序，随机逻辑可能失效")
	}

	// 标记 100% 丢失：快照不能完成，必须如实标记
	req := &simulator.Request{
		Seed: 1, TickLimit: 40,
		Nodes: []simulator.NodeSpec{{Name: "A", Balance: 100}, {Name: "B", Balance: 100}},
		Links: []simulator.LinkSpec{
			{From: "A", To: "B", Policy: simulator.PolicyBestEffort, Base: 2, LossPct: 100},
			{From: "B", To: "A", Policy: simulator.PolicyBestEffort, Base: 2},
		},
		Events: []simulator.Event{
			{Kind: "marker", Tick: 1, From: "A", SnapshotID: "lost"},
		},
	}
	resp, err := simulator.Execute(req)
	if err != nil {
		t.Fatal(err)
	}
	s := findSnap(t, resp, "lost")
	if s.Complete {
		t.Fatal("标记丢失时快照不应完成")
	}
	if len(s.Missing) == 0 {
		t.Fatal("不完整快照必须列出缺失项")
	}
}

// ---- 确定性：同 seed 两次运行结果逐字节一致；不同 seed 也一致（无全局随机源）----

func TestDeterminism(t *testing.T) {
	req := &simulator.Request{
		Seed: 99, TickLimit: 40,
		Nodes: []simulator.NodeSpec{{Name: "A", Balance: 100}, {Name: "B", Balance: 100}},
		Links: []simulator.LinkSpec{
			{From: "A", To: "B", Policy: simulator.PolicyBestEffort, Base: 2, Jitter: 2, DupPct: 30, LossPct: 10, Reorder: true},
			{From: "B", To: "A", Policy: simulator.PolicyBestEffort, Base: 2, Jitter: 2, DupPct: 30, LossPct: 10, Reorder: true},
		},
		Events: []simulator.Event{
			{Kind: "transfer", Tick: 0, From: "A", To: "B", Amount: 3, RepeatTo: 20, RepeatEvery: 1},
			{Kind: "transfer", Tick: 0, From: "B", To: "A", Amount: 2, RepeatTo: 20, RepeatEvery: 1},
			{Kind: "marker", Tick: 5, From: "A", SnapshotID: "d1"},
		},
	}
	run := func() string {
		resp, err := simulator.Execute(req)
		if err != nil {
			t.Fatal(err)
		}
		b, _ := json.Marshal(resp)
		return string(b)
	}
	a, b := run(), run()
	if a != b {
		t.Fatal("相同输入两次运行结果不一致，模拟器不确定")
	}
}

// ---- 输入校验 ----

func TestValidation(t *testing.T) {
	good := &simulator.Request{
		Nodes:  []simulator.NodeSpec{{Name: "A", Balance: 1}, {Name: "B", Balance: 1}},
		Links:  []simulator.LinkSpec{{From: "A", To: "B", Policy: "fifo"}},
		Events: []simulator.Event{{Kind: "transfer", Tick: 0, From: "A", To: "B", Amount: 1}},
	}
	mkBad := func(mut func(*simulator.Request)) *simulator.Request {
		r := *good
		r.Nodes = append([]simulator.NodeSpec(nil), good.Nodes...)
		r.Links = append([]simulator.LinkSpec(nil), good.Links...)
		r.Events = append([]simulator.Event(nil), good.Events...)
		mut(&r)
		return &r
	}

	cases := []struct {
		name string
		req  *simulator.Request
		want string
	}{
		{"无节点", mkBad(func(r *simulator.Request) { r.Nodes = nil }), "节点"},
		{"重名", mkBad(func(r *simulator.Request) { r.Nodes = append(r.Nodes, simulator.NodeSpec{Name: "A"}) }), "重复"},
		{"缺链路", mkBad(func(r *simulator.Request) { r.Links = nil }), "链路"},
		{"金额非正", mkBad(func(r *simulator.Request) { r.Events[0].Amount = 0 }), "amount"},
		{"未知策略", mkBad(func(r *simulator.Request) { r.Links[0].Policy = "udp" }), "policy"},
		{"百分比越界", mkBad(func(r *simulator.Request) { r.Links[0].LossPct = 200 }), "[0,100]"},
		{"marker 缺 id", mkBad(func(r *simulator.Request) {
			r.Events = []simulator.Event{{Kind: "marker", Tick: 1, From: "A"}}
		}), "snapshot_id"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := simulator.Execute(c.req)
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("期望错误含 %q, got %v", c.want, err)
			}
		})
	}
}

// ---- examples/ 下的样例文件全部可运行，且 FIFO 样例快照守恒 ----

func TestExampleRequests(t *testing.T) {
	files, err := filepath.Glob(filepath.Join("..", "..", "examples", "*.json"))
	if err != nil || len(files) == 0 {
		t.Skip("未找到 examples 目录")
	}
	for _, f := range files {
		name := filepath.Base(f)
		t.Run(name, func(t *testing.T) {
			raw, err := os.ReadFile(f)
			if err != nil {
				t.Fatal(err)
			}
			var req simulator.Request
			if err := json.Unmarshal(raw, &req); err != nil {
				t.Fatal(err)
			}
			resp, err := simulator.Execute(&req)
			if err != nil {
				t.Fatal(err)
			}
			// best_effort 演示样例允许不守恒/不完成；其余样例快照必须守恒
			if !strings.Contains(name, "best-effort") {
				total := totalMoney(&req)
				for _, s := range resp.Snapshots {
					if s.Complete && s.Total != total {
						t.Fatalf("样例 %s 快照 %s 总量 %d != 初始 %d", name, s.ID, s.Total, total)
					}
				}
			}
		})
	}
}
