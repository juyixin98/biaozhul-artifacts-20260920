package sim

import (
	"testing"

	"simsnap/internal/msg"
)

// TestEngineDeterministic 验证同种子下事件序列与随机数完全可复现。
func TestEngineDeterministic(t *testing.T) {
	run := func(seed int64) []int {
		eng := NewEngine(seed)
		var out []int
		eng.Schedule(5, 0, func(int64) { out = append(out, int(eng.RNG().Intn(1000))) })
		eng.Schedule(2, 0, func(int64) { out = append(out, int(eng.RNG().Intn(1000))) })
		eng.Schedule(2, 0, func(int64) { out = append(out, int(eng.RNG().Intn(1000))) })
		eng.Schedule(8, 0, func(int64) { out = append(out, int(eng.RNG().Intn(1000))) })
		eng.Run()
		return out
	}
	a := run(int64(123))
	b := run(int64(123))
	if len(a) != 4 || len(b) != 4 {
		t.Fatalf("事件数不符: %d %d", len(a), len(b))
	}
	for i := range a {
		if a[i] != b[i] {
			t.Fatalf("同种子结果不一致: %v vs %v", a, b)
		}
	}
	c := run(int64(124))
	same := true
	for i := range a {
		if a[i] != c[i] {
			same = false
		}
	}
	if same {
		t.Fatalf("不同种子不应得到完全相同序列: %v", a)
	}
	// 时间顺序：t=2 两个先，再 t=5，再 t=8。
	// 同刻两事件的 RNG 值已按调度顺序进入 out，这里只验证长度与可复现性。
}

// TestEngineStopAndDeadline 验证 Stop 与截止时刻。
func TestEngineStopAndDeadline(t *testing.T) {
	eng := NewEngine(1)
	fired := 0
	eng.Schedule(10, 0, func(int64) { fired++ })
	eng.Schedule(3, 0, func(int64) { fired++; eng.Stop() })
	eng.Schedule(5, 0, func(int64) { fired++ })
	eng.Run()
	if fired != 1 {
		t.Fatalf("Stop 后不应再派发 t=5/t=10 事件, fired=%d", fired)
	}
	if eng.Pending() != 2 {
		t.Fatalf("剩余事件应为 2, got %d", eng.Pending())
	}

	eng2 := NewEngine(1)
	eng2.SetDeadline(4)
	ran := 0
	eng2.Schedule(2, 0, func(int64) { ran++ })
	eng2.Schedule(10, 0, func(int64) { ran++ })
	eng2.Run()
	if ran != 1 {
		t.Fatalf("截止时刻后事件不应执行, ran=%d", ran)
	}
}

// TestNetworkFIFO 验证 FIFO 链路下消息严格按发送顺序到达。
func TestNetworkFIFO(t *testing.T) {
	eng := NewEngine(7)
	nw := NewNetwork(eng)
	nw.AddLink(0, 1, ReliableFIFO(1, 50))
	var got []int
	deliver := func(d Delivery) { got = append(got, d.Msg.Amount) }
	for i := 1; i <= 100; i++ {
		nw.Send(msg.Message{Kind: msg.Data, From: 0, To: 1, Amount: i}, deliver)
	}
	eng.Run()
	if len(got) != 100 {
		t.Fatalf("可靠链路应收齐 100 条, got %d", len(got))
	}
	for i, v := range got {
		if v != i+1 {
			t.Fatalf("FIFO 被破坏: 位置 %d 收到 %d", i, v)
		}
	}
}

// TestNetworkNonFIFOReorders 白盒验证非 FIFO + 大抖动会产生乱序。
func TestNetworkNonFIFOReorders(t *testing.T) {
	eng := NewEngine(1)
	nw := NewNetwork(eng)
	nw.AddLink(0, 1, Policy{MinDelay: 1, MaxDelay: 40, FIFO: false})
	var got []int
	deliver := func(d Delivery) { got = append(got, d.Msg.Amount) }
	for i := 1; i <= 60; i++ {
		nw.Send(msg.Message{Kind: msg.Data, From: 0, To: 1, Amount: i}, deliver)
	}
	eng.Run()
	ordered := true
	for i, v := range got {
		if v != i+1 {
			ordered = false
		}
	}
	if ordered {
		t.Fatalf("非 FIFO 大抖动下预期出现乱序，实际完全有序: %v", got[:10])
	}
	if nw.Stats(0, 1).Reordered == 0 {
		t.Fatalf("reordered 统计应 > 0")
	}
}

// TestNetworkLossAndDup 验证丢包率与重复率统计。
func TestNetworkLossAndDup(t *testing.T) {
	eng := NewEngine(999)
	nw := NewNetwork(eng)
	nw.AddLink(0, 1, Policy{MinDelay: 1, MaxDelay: 1, LossPct: 50, DupPct: 50, FIFO: true})
	deliver := func(Delivery) {}
	for i := 0; i < 1000; i++ {
		nw.Send(msg.Message{Kind: msg.Data, From: 0, To: 1, Amount: i}, deliver)
	}
	eng.Run()
	st := nw.Stats(0, 1)
	if st.Lost == 0 || st.Lost >= 1000 {
		t.Fatalf("丢包统计异常: %+v", st)
	}
	if st.Sent != 1000 {
		t.Fatalf("sent 应为 1000, got %d", st.Sent)
	}
	if st.Delivered != 1000-st.Lost+st.Duplicated {
		t.Fatalf("delivered/lost/dup 数量关系不成立: %+v", st)
	}
}
