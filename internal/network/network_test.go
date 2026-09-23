package network

import (
	"math/rand"
	"testing"

	"raftdemo/internal/raft"
)

func mkMsg(from, to, seq int) raft.Msg {
	// 用 Entries.Data 携带序号，仅为测试区分消息。
	return raft.Msg{Type: raft.MsgAppReq, Term: 1, From: from, To: to}
}

// deliverTicks 发送 total 条消息并记录每条消息的投递 tick 分布。
func deliverTicks(seed int64, link *Link, total int) []int {
	nw := New(3, rand.New(rand.NewSource(seed)))
	nw.SetLink(0, 1, link)
	for i := 0; i < total; i++ {
		nw.Send(0, mkMsg(0, 1, i))
	}
	var out []int
	for t := 1; t < 100; t++ {
		ms := nw.Tick(t)
		for range ms {
			out = append(out, t)
		}
	}
	return out
}

func TestDeterministicDelivery(t *testing.T) {
	link := &Link{DelayTicks: 2, Jitter: 5}
	a := deliverTicks(123, link, 50)
	b := deliverTicks(123, link, 50)
	if len(a) != len(b) {
		t.Fatalf("两次运行投递数量不同: %d vs %d", len(a), len(b))
	}
	for i := range a {
		if a[i] != b[i] {
			t.Fatalf("同种子投递时刻不确定: 位置 %d: %d vs %d", i, a[i], b[i])
		}
	}
	for _, at := range a {
		if at < 2 || at > 6 {
			t.Fatalf("投递时刻超出 [2,6] 窗口: %d", at)
		}
	}
}

func TestLossAndDuplicate(t *testing.T) {
	// 全丢。
	ticks := deliverTicks(1, &Link{DelayTicks: 1, LossRate: 1}, 100)
	if len(ticks) != 0 {
		t.Fatalf("LossRate=1 仍有消息投递: %d", len(ticks))
	}
	// 全部重复：每条恰好两份。
	nw := New(3, rand.New(rand.NewSource(1)))
	nw.SetLink(0, 1, &Link{DelayTicks: 1, DupRate: 1})
	for i := 0; i < 100; i++ {
		nw.Send(0, mkMsg(0, 1, i))
	}
	got := 0
	for tick := 1; tick < 50; tick++ {
		got += len(nw.Tick(tick))
	}
	if got != 200 {
		t.Fatalf("DupRate=1 应产生 200 份投递，实际 %d", got)
	}
	if nw.Duplicated() != 100 {
		t.Fatalf("重复计数错误: %d", nw.Duplicated())
	}
}

func TestPartitionIsolateReset(t *testing.T) {
	nw := New(3, rand.New(rand.NewSource(0)))
	nw.Isolate(0, false)
	nw.Send(0, mkMsg(0, 1, 0))
	nw.Send(1, mkMsg(1, 0, 0))
	nw.Send(1, mkMsg(1, 2, 0))
	if nw.Dropped() != 2 {
		t.Fatalf("隔离 0 后跨区消息应被丢弃，实际 dropped=%d", nw.Dropped())
	}
	nw.Isolate(0, true)
	nw.Send(0, mkMsg(0, 1, 0))
	if nw.Dropped() != 2 {
		t.Fatalf("恢复隔离后不应再丢消息，dropped=%d", nw.Dropped())
	}
	// partition: {0,1} 与 {2} 分裂。
	nw.SetPartition([]int{0, 0, 1}, false)
	nw.Send(0, mkMsg(0, 2, 0))
	nw.Send(2, mkMsg(2, 1, 0))
	nw.Send(0, mkMsg(0, 1, 0))
	if nw.Dropped() != 4 {
		t.Fatalf("分区跨区消息应丢弃，dropped=%d", nw.Dropped())
	}
	nw.SetPartition([]int{0, 0, 1}, true)
	nw.Send(0, mkMsg(0, 2, 0))
	if nw.Dropped() != 4 {
		t.Fatalf("分区愈合后不应再丢消息，dropped=%d", nw.Dropped())
	}
}

func TestReorderPossibleAndDeterministic(t *testing.T) {
	// 大抖动下统计投递 tick 方差：存在乱序可能，且两次运行完全一致。
	link := &Link{DelayTicks: 1, Jitter: 20}
	a := deliverTicks(7, link, 200)
	uniq := map[int]bool{}
	for _, at := range a {
		uniq[at] = true
	}
	if len(uniq) < 5 {
		t.Fatalf("jitter=20 却几乎没有投递时刻离散度: %v", uniq)
	}
	b := deliverTicks(7, link, 200)
	for i := range a {
		if a[i] != b[i] {
			t.Fatal("乱序随机过程不确定")
		}
	}
}
