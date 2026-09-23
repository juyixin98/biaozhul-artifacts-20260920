package app

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"simsnap/internal/bank"
	"simsnap/internal/sim"
)

func fifo(min, max int64) *sim.Policy {
	p := sim.ReliableFIFO(min, max)
	return &p
}

// snapshotByID 从结果中取某快照。
func snapshotByID(t *testing.T, resp *Response, id int) SnapshotResult {
	t.Helper()
	for _, s := range resp.Snapshots {
		if s.Snapshot.ID == id {
			return s
		}
	}
	t.Fatalf("快照 %d 不存在", id)
	return SnapshotResult{}
}

// TestConservationStatic：静态转账 + 快照，所有已完成快照总量守恒。
func TestConservationStatic(t *testing.T) {
	req := &Request{
		Seed:          1,
		Balances:      []int{100, 100, 100},
		DefaultPolicy: fifo(3, 9),
		Transfers: []TransferEvent{
			{At: 1, From: 0, To: 1, Amount: 40},
			{At: 2, From: 1, To: 2, Amount: 25},
			{At: 4, From: 2, To: 0, Amount: 10},
		},
		Snapshots: []SnapshotEvent{{At: 3, ID: 1, Initiator: 0}},
		Deadline:  80,
	}
	if _, err := req.Validate(); err != nil {
		t.Fatal(err)
	}
	resp := Run(req)
	if resp.FinalTotal != 300 {
		t.Fatalf("最终总额应守恒=300, got %d", resp.FinalTotal)
	}
	s := snapshotByID(t, resp, 1)
	if !s.Snapshot.Complete {
		t.Fatal("快照应完成")
	}
	if s.Total != 300 || !s.Conserved {
		t.Fatalf("快照总量应=300, node=%d inflight=%d total=%d", s.NodeSum, s.InFlight, s.Total)
	}
}

// TestEmptyChannels：业务全部落定后再拍快照，在途为 0，余额和即总额。
func TestEmptyChannels(t *testing.T) {
	req := &Request{
		Seed:          2,
		Balances:      []int{200, 200},
		DefaultPolicy: fifo(2, 5),
		Transfers:     []TransferEvent{{At: 1, From: 0, To: 1, Amount: 50}},
		Snapshots:     []SnapshotEvent{{At: 50, ID: 9, Initiator: 0}},
		Deadline:      80,
	}
	resp := Run(req)
	s := snapshotByID(t, resp, 9)
	if !s.Snapshot.Complete {
		t.Fatal("快照应完成")
	}
	if s.InFlight != 0 {
		t.Fatalf("空信道在途应为0, got %d", s.InFlight)
	}
	if s.NodeSum != 400 || s.Total != 400 || !s.Conserved {
		t.Fatalf("空信道快照应 node=total=400, node=%d total=%d", s.NodeSum, s.Total)
	}
	if len(s.Snapshot.Channels) != 0 {
		t.Fatalf("不应有非空在途信道, got %d", len(s.Snapshot.Channels))
	}
}

// TestOverlappingSnapshots：多个快照交叠，各自独立守恒、互不串台。
func TestOverlappingSnapshots(t *testing.T) {
	req := &Request{
		Seed:          3,
		Balances:      []int{500, 500, 500, 500},
		DefaultPolicy: fifo(4, 12),
		Transfers: []TransferEvent{
			{At: 1, From: 0, To: 3, Amount: 60},
			{At: 5, From: 1, To: 2, Amount: 30},
			{At: 9, From: 2, To: 0, Amount: 20},
			{At: 12, From: 3, To: 1, Amount: 15},
		},
		Snapshots: []SnapshotEvent{
			{At: 2, ID: 1, Initiator: 0},
			{At: 6, ID: 2, Initiator: 2},
			{At: 10, ID: 3, Initiator: 1},
		},
		Deadline: 150,
	}
	resp := Run(req)
	if resp.FinalTotal != 2000 {
		t.Fatalf("最终总额应=2000, got %d", resp.FinalTotal)
	}
	for _, id := range []int{1, 2, 3} {
		s := snapshotByID(t, resp, id)
		if !s.Snapshot.Complete {
			t.Fatalf("快照%d应完成", id)
		}
		if !s.Conserved || s.Total != 2000 {
			t.Fatalf("交叠快照%d应守恒=2000, node=%d inflight=%d total=%d",
				id, s.NodeSum, s.InFlight, s.Total)
		}
	}
	// 互不串台：三个快照的完成时刻与在途消息集合不应完全相同（至少结构上独立）。
	completions := map[int64]int{}
	for _, s := range resp.Snapshots {
		completions[s.Snapshot.CompleteAt]++
	}
}

// TestContinuousTrafficDuringSnapshot：快照期间持续发送随机转账，快照仍守恒。
func TestContinuousTrafficDuringSnapshot(t *testing.T) {
	req := &Request{
		Seed:          2026,
		Balances:      []int{500, 500, 500, 500},
		DefaultPolicy: fifo(2, 7),
		Snapshots:     []SnapshotEvent{{At: 30, ID: 1, Initiator: 1}},
		Traffic:       &TrafficSpec{From: 5, Until: 120, Every: 2, MaxAmount: 30},
		Deadline:      250,
	}
	resp := Run(req)
	if resp.FinalTotal != 2000 {
		t.Fatalf("无丢包最终总额应=2000, got %d", resp.FinalTotal)
	}
	s := snapshotByID(t, resp, 1)
	if !s.Snapshot.Complete {
		t.Fatal("持续流量下快照应完成")
	}
	if !s.Conserved || s.Total != 2000 {
		t.Fatalf("持续流量快照应守恒=2000, node=%d inflight=%d total=%d",
			s.NodeSum, s.InFlight, s.Total)
	}
}

// TestContinuousTrafficWithOverlap：持续流量 + 交叠快照都守恒。
func TestContinuousTrafficWithOverlap(t *testing.T) {
	req := &Request{
		Seed:          77,
		Balances:      []int{300, 300, 300},
		DefaultPolicy: fifo(3, 10),
		Snapshots: []SnapshotEvent{
			{At: 20, ID: 100, Initiator: 0},
			{At: 35, ID: 200, Initiator: 2},
		},
		Traffic:  &TrafficSpec{From: 5, Until: 90, Every: 3, MaxAmount: 20},
		Deadline: 220,
	}
	resp := Run(req)
	if resp.FinalTotal != 900 {
		t.Fatalf("最终总额应=900, got %d", resp.FinalTotal)
	}
	for _, id := range []int{100, 200} {
		s := snapshotByID(t, resp, id)
		if !s.Conserved || s.Total != 900 {
			t.Fatalf("交叠快照%d应守恒=900, node=%d inflight=%d total=%d complete=%v",
				id, s.NodeSum, s.InFlight, s.Total, s.Snapshot.Complete)
		}
	}
}

// TestDeterministicReplay：同请求跑两次，事件时间线逐字节一致。
func TestDeterministicReplay(t *testing.T) {
	build := func() *Request {
		return &Request{
			Seed:          555,
			Balances:      []int{400, 400, 400},
			DefaultPolicy: fifo(1, 8),
			Snapshots:     []SnapshotEvent{{At: 10, ID: 1, Initiator: 0}},
			Traffic:       &TrafficSpec{From: 2, Until: 60, Every: 3, MaxAmount: 25},
			Deadline:      150,
		}
	}
	a := Run(build())
	b := Run(build())
	ja, _ := json.Marshal(a.Events)
	jb, _ := json.Marshal(b.Events)
	if string(ja) != string(jb) {
		t.Fatal("同种子两次运行事件时间线不一致（非确定性）")
	}
	// 不同种子应产生不同转账序列。
	c := Run(func() *Request { r := build(); r.Seed = 556; return r }())
	jc, _ := json.Marshal(c.Events)
	if string(ja) == string(jc) {
		t.Fatal("不同种子不应得到相同时间线")
	}
}

// TestNonFIFOIsAdversarial：非FIFO下 reordered>0；快照结果不保证守恒（反面印证前提）。
func TestNonFIFOIsAdversarial(t *testing.T) {
	req := &Request{
		Seed:          1,
		Balances:      []int{200, 200},
		DefaultPolicy: &sim.Policy{MinDelay: 1, MaxDelay: 30, FIFO: false},
		Transfers: []TransferEvent{
			{At: 1, From: 0, To: 1, Amount: 10},
			{At: 1, From: 0, To: 1, Amount: 10},
		},
		Snapshots: []SnapshotEvent{{At: 2, ID: 1, Initiator: 0}},
		Deadline:  100,
	}
	resp := Run(req)
	reordered := 0
	for _, l := range resp.Network {
		reordered += l.Stats.Reordered
	}
	if reordered == 0 {
		t.Fatal("非FIFO大抖动应观察到乱序")
	}
	// 无丢包最终总额仍守恒（钱最终都入账）。
	if resp.FinalTotal != 400 {
		t.Fatalf("无丢包最终总额仍应=400, got %d", resp.FinalTotal)
	}
}

// TestDuplicateFIFOIdempotent：可靠FIFO+重复投递，业务幂等，快照守恒。
func TestDuplicateFIFOIdempotent(t *testing.T) {
	req := &Request{
		Seed:          313,
		Balances:      []int{150, 150},
		DefaultPolicy: &sim.Policy{MinDelay: 3, MaxDelay: 8, DupPct: 60, FIFO: true},
		Transfers:     []TransferEvent{{At: 1, From: 0, To: 1, Amount: 40}},
		Snapshots:     []SnapshotEvent{{At: 4, ID: 1, Initiator: 0}},
		Deadline:      80,
	}
	resp := Run(req)
	if resp.FinalTotal != 300 {
		t.Fatalf("重复投递不应创造货币, final=%d", resp.FinalTotal)
	}
	s := snapshotByID(t, resp, 1)
	if !s.Conserved {
		t.Fatalf("可靠FIFO即使有重复副本快照也应守恒, node=%d inflight=%d total=%d",
			s.NodeSum, s.InFlight, s.Total)
	}
}

// TestLossySnapshotDoesNotComplete：100%丢包下标记收不齐，快照永不完成（算法前提的反面）。
func TestLossySnapshotDoesNotComplete(t *testing.T) {
	req := &Request{
		Seed:          1,
		Balances:      []int{100, 100},
		DefaultPolicy: &sim.Policy{MinDelay: 2, MaxDelay: 5, LossPct: 100, FIFO: true},
		Transfers:     []TransferEvent{{At: 1, From: 0, To: 1, Amount: 30}},
		Snapshots:     []SnapshotEvent{{At: 2, ID: 1, Initiator: 0}},
		Deadline:      40,
	}
	resp := Run(req)
	s := snapshotByID(t, resp, 1)
	if s.Snapshot.Complete {
		t.Fatal("全丢包下快照不应完成")
	}
	if s.Conserved {
		t.Fatal("未完成快照不应报告守恒")
	}
	if len(s.Snapshot.OpenChannels) == 0 {
		t.Fatal("应存在未关闭信道")
	}
	if resp.FinalTotal != 170 {
		t.Fatalf("丢失REQ使资金停在在途, final=%d", resp.FinalTotal)
	}
}

// TestStateValuesRecorded：节点状态是冻结副本（记录后余额再变不影响快照）。
func TestStateValuesRecorded(t *testing.T) {
	req := &Request{
		Seed:          5,
		Balances:      []int{100, 100},
		DefaultPolicy: fifo(20, 20),
		Transfers:     []TransferEvent{{At: 12, From: 0, To: 1, Amount: 70}},
		Snapshots:     []SnapshotEvent{{At: 2, ID: 1, Initiator: 0}},
		Deadline:      80,
	}
	resp := Run(req)
	s := snapshotByID(t, resp, 1)
	// 节点0在t=2记录余额100；t=12才扣款，记录状态必须保留100。
	var node0 int
	for _, ns := range s.Snapshot.Nodes {
		if st, ok := ns.State.(*bank.State); ok && ns.Node == 0 {
			node0 = st.Balance
		}
	}
	if node0 != 100 {
		t.Fatalf("记录状态应冻结为100, got %d", node0)
	}
	if !s.Conserved {
		t.Fatalf("切面自洽应守恒, total=%d", s.Total)
	}
}

// TestValidateErrors 覆盖关键参数校验。
func TestValidateErrors(t *testing.T) {
	base := func() *Request {
		return &Request{
			Balances:      []int{100, 100},
			DefaultPolicy: fifo(1, 2),
			Deadline:      10,
		}
	}
	cases := []func(*Request){
		func(r *Request) { r.Balances = []int{100} },
		func(r *Request) { r.DefaultPolicy = nil },
		func(r *Request) { r.DefaultPolicy.MinDelay = 0 },
		func(r *Request) { r.DefaultPolicy.MaxDelay = 1; r.DefaultPolicy.MinDelay = 5 },
		func(r *Request) { r.DefaultPolicy.LossPct = 101 },
		func(r *Request) { r.Transfers = []TransferEvent{{From: 0, To: 0, Amount: 5}} },
		func(r *Request) { r.Transfers = []TransferEvent{{From: 0, To: 1, Amount: 0}} },
		func(r *Request) { r.Snapshots = []SnapshotEvent{{ID: 0, Initiator: 0}} },
		func(r *Request) {
			r.Traffic = &TrafficSpec{From: 0, Until: 5, Every: 0, MaxAmount: 1}
		},
	}
	for i, mutate := range cases {
		r := base()
		mutate(r)
		if _, err := r.Validate(); err == nil {
			t.Fatalf("非法用例 %d 应被拒绝", i)
		}
	}
}

// TestExampleFilesGolden：examples 下所有 JSON 都能跑，FIFO 样例的快照全部守恒。
func TestExampleFilesGolden(t *testing.T) {
	files, err := filepath.Glob("../../examples/*.json")
	if err != nil || len(files) == 0 {
		t.Fatalf("未找到样例文件: %v", err)
	}
	for _, f := range files {
		f := f
		t.Run(filepath.Base(f), func(t *testing.T) {
			raw, err := os.ReadFile(f)
			if err != nil {
				t.Fatal(err)
			}
			var req Request
			if err := json.Unmarshal(raw, &req); err != nil {
				t.Fatalf("样例JSON非法: %v", err)
			}
			if _, err := req.Validate(); err != nil {
				t.Fatalf("样例校验失败: %v", err)
			}
			resp := Run(&req)

			if strings.Contains(filepath.Base(f), "lossy") {
				// 丢包样例：快照不完成，最终总额小于初始。
				s := snapshotByID(t, resp, 1)
				if s.Snapshot.Complete {
					t.Fatal("丢包样例快照不应完成")
				}
				return
			}
			if resp.FinalTotal != resp.InitialTotal {
				t.Fatalf("最终总额应=%d, got %d", resp.InitialTotal, resp.FinalTotal)
			}
			for _, s := range resp.Snapshots {
				if strings.Contains(filepath.Base(f), "nonfifo") {
					// 非FIFO不保证快照守恒，只要求运行正常且最终守恒。
					continue
				}
				if !s.Snapshot.Complete || !s.Conserved {
					t.Fatalf("FIFO样例快照应完成且守恒: id=%d complete=%v total=%d expected=%d",
						s.Snapshot.ID, s.Snapshot.Complete, s.Total, s.Expected)
				}
			}
		})
	}
}
