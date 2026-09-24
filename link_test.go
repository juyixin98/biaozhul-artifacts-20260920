package rt

import (
	"context"
	"sync"
	"testing"
)

// 直接驱动注入器，验证丢包/重复/乱序/幽灵四类故障。
func TestInjectorFaults(t *testing.T) {
	var mu sync.Mutex
	var got []*Packet
	deliver := func(p *Packet) {
		mu.Lock()
		got = append(got, p)
		mu.Unlock()
	}
	ghost := &Packet{Type: MsgDATA, Gen: 41, Seq: 1}
	pol := FaultPolicy{
		Seed:          12345,
		LossRate:      1, // 必中
		LossBudget:    2,
		DupRate:       1,
		DupBudget:     3,
		ReorderRate:   1,
		ReorderBudget: 2,
		ReorderHold:   2,
		Ghosts:        []*Packet{ghost, ghost},
	}
	inj := NewInjector(pol, RealClock{}, deliver)

	// 发 6 个报文后冲刷。
	for i := 0; i < 6; i++ {
		inj.Egress(&Packet{Type: MsgDATA, Gen: 42, Seq: uint32(i)})
	}
	inj.Flush()

	st := inj.Stats()
	if st.Lost != 2 {
		t.Errorf("Lost=%d want 2", st.Lost)
	}
	if st.Reordered != 2 {
		t.Errorf("Reordered=%d want 2", st.Reordered)
	}
	if st.GhostSent != 2 {
		t.Errorf("GhostSent=%d want 2", st.GhostSent)
	}
	if st.Duplicated != 3 {
		t.Errorf("Duplicated=%d want 3", st.Duplicated)
	}
	// 预算耗尽后丢包不再发生：再发 5 个，全部至少投递一份。
	before := st.Delivered
	for i := 0; i < 5; i++ {
		inj.Egress(&Packet{Type: MsgDATA, Gen: 42, Seq: uint32(10 + i)})
	}
	inj.Flush()
	st2 := inj.Stats()
	if st2.Lost != 2 {
		t.Errorf("loss budget exceeded: Lost=%d", st2.Lost)
	}
	if st2.Delivered-before < 5 {
		t.Errorf("post-budget delivery short: %d", st2.Delivered-before)
	}
	// 幽灵代际必须是旧代际（41 < 42），且确实送达过。
	mu.Lock()
	sawGhost := false
	for _, p := range got {
		if p.Gen == 41 {
			sawGhost = true
		}
	}
	mu.Unlock()
	if !sawGhost {
		t.Error("ghost packet was never delivered")
	}
}

// 零故障策略：报文原样保序通过。
func TestInjectorClean(t *testing.T) {
	var got []*Packet
	inj := NewInjector(FaultPolicy{}, RealClock{}, func(p *Packet) { got = append(got, p) })
	for i := 0; i < 10; i++ {
		inj.Egress(&Packet{Seq: uint32(i), Gen: 1})
	}
	if len(got) != 10 {
		t.Fatalf("got %d packets, want 10", len(got))
	}
	for i, p := range got {
		if p.Seq != uint32(i) {
			t.Fatalf("order broken at %d: seq %d", i, p.Seq)
		}
	}
}

// Pipe 的关闭语义：取完缓冲后 Recv 返回 ErrLinkClosed。
func TestPipeClose(t *testing.T) {
	a, b := Pipe(PipeFaults{})
	_ = a.Send(&Packet{Type: MsgDATA, Seq: 1, Gen: 1})
	p, err := b.Recv(context.Background())
	if err != nil || p.Seq != 1 {
		t.Fatalf("recv before close: %v %v", p, err)
	}
	_ = b.Close()
	if _, err := b.Recv(context.Background()); err != ErrLinkClosed {
		t.Fatalf("after close err=%v want ErrLinkClosed", err)
	}
	// ctx 取消优先返回 ctx 错误。
	a2, _ := Pipe(PipeFaults{})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := a2.Recv(ctx); err == nil {
		t.Fatal("canceled ctx should return an error")
	}
}
