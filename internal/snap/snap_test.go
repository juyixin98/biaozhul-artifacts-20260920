package snap

import (
	"testing"

	"simsnap/internal/msg"
)

// testEnv 构造最小 3 节点环境（全互联）用于白盒测试。
type testEnv struct {
	mgr    *Manager
	states map[int]int
}

func newTestEnv() *testEnv {
	e := &testEnv{states: map[int]int{0: 100, 1: 100, 2: 100}}
	e.mgr = NewManager(3,
		func(int, int, int) {}, // 标记发送在白盒测试中忽略（直接驱动 MarkerReceived）
		func(node int) any { return e.states[node] },
	)
	return e
}

func dm(from, to, amount int, txn, sentAt int64) msg.Message {
	return msg.Message{Kind: msg.Data, From: from, To: to, Amount: amount, TxnID: txn, SentAt: sentAt}
}

// closeAll 标记快照全部入信道关闭（模拟所有标记都已收到），触发完成。
func closeAll(m *Manager, id int) {
	for b := 0; b < 3; b++ {
		for a := 0; a < 3; a++ {
			if a != b {
				m.MarkerReceived(id, a, b, 100)
			}
		}
	}
}

// TestBasicInFlight：记录状态之后、入信道标记之前到达的消息进入在途记录。
func TestBasicInFlight(t *testing.T) {
	e := newTestEnv()
	m := e.mgr
	m.Initiate(1, 0, 10) // 0 在 t=10 记录并发标记

	// 1、2 分别收到 0 的标记 => 记录状态。
	m.MarkerReceived(1, 0, 1, 12)
	m.MarkerReceived(1, 0, 2, 12)

	// 此时 1->0 信道尚未关闭：1->0 的转账到达节点0 => 在途。
	msg := dm(1, 0, 30, 1, 13)
	m.NoteSend(msg)
	m.NoteDelivery(msg, 13)
	if cap := m.DataReceived(1, 0, msg); len(cap) != 1 || cap[0] != 1 {
		t.Fatalf("消息应被快照1捕获, got %v", cap)
	}

	// 标记到齐后快照完成。
	closeAll(m, 1)
	if !m.IsComplete(1) {
		t.Fatal("快照应完成")
	}
	s := m.Build(1)
	if len(s.Channels) != 1 || s.Channels[0].From != 1 || s.Channels[0].To != 0 ||
		len(s.Channels[0].Msgs) != 1 || s.Channels[0].Msgs[0].Amount != 30 {
		t.Fatalf("在途信道记录错误: %+v", s.Channels)
	}
}

// TestOverlappingIsolation：两个快照交叠，同一条/不同消息各归其主，互不串台。
func TestOverlappingIsolation(t *testing.T) {
	e := newTestEnv()
	m := e.mgr

	// 快照1：0 在 t=0 发起。
	m.Initiate(1, 0, 0)
	// 快照2：1 在 t=10 发起（快照1仍在进行）。
	m.Initiate(2, 1, 10)

	// 节点0 在 t=11 收到快照2来自1的标记 => 记录快照2。
	m.MarkerReceived(2, 1, 0, 11)

	// 消息 A：t=5 到达节点0。此时：
	//   - 节点0 已记录快照1（t=0），快照1信道1->0未关 => 归快照1；
	//   - 节点0 尚未记录快照2（t=11）=> 不归快照2（将进快照2的节点0余额）。
	a := dm(1, 0, 11, 1, 5)
	m.NoteSend(a)
	m.NoteDelivery(a, 5)
	if cap := m.DataReceived(1, 0, a); len(cap) != 1 || cap[0] != 1 {
		t.Fatalf("消息A应只归快照1, got %v", cap)
	}

	// 节点0 在 t=12 收到快照1来自1的标记 => 关闭快照1信道1->0。
	m.MarkerReceived(1, 1, 0, 12)

	// 消息 B：t=13 到达节点0。快照1信道1->0已关（不归1）；
	// 快照2信道1->0未关（节点0 t=11记录，标记2 1->0就是t=11那条，同方向快照2信道也已关！）
	// 快照2中 1->0 的标记在 t=11 已收到 => 该信道已关闭，故 B 不归任何快照（cut之后）。
	b := dm(1, 0, 22, 2, 13)
	m.NoteSend(b)
	m.NoteDelivery(b, 13)
	if cap := m.DataReceived(1, 0, b); len(cap) != 0 {
		t.Fatalf("消息B在两信道关闭后到达, 不应被捕获, got %v", cap)
	}

	// 用 2->0 方向构造只归快照2的消息：节点0已记录快照2，2->0 标记2未到；
	// 快照1的2->0标记也未到 => 消息C同时被两个快照捕获（它对两个切面都在途）。
	c := dm(2, 0, 33, 7, 13)
	m.NoteSend(c)
	m.NoteDelivery(c, 13)
	capC := m.DataReceived(2, 0, c)
	if len(capC) != 2 {
		t.Fatalf("消息C应同时处于两个快照的在途窗口, got %v", capC)
	}

	// 完成两个快照。
	closeAll(m, 1)
	closeAll(m, 2)
	s1 := m.Build(1)
	s2 := m.Build(2)

	// 快照1在途：1->0 的 A(11) 与 2->0 的 C(33)。
	got1 := map[[2]int]int{}
	for _, ch := range s1.Channels {
		sum := 0
		for _, mm := range ch.Msgs {
			sum += mm.Amount
		}
		got1[[2]int{ch.From, ch.To}] = sum
	}
	if got1[[2]int{1, 0}] != 11 || got1[[2]int{2, 0}] != 33 {
		t.Fatalf("快照1在途归属错误: %+v", got1)
	}

	// 快照2在途：只有 2->0 的 C(33)；A 在节点0记录快照2之前到，进余额。
	got2 := map[[2]int]int{}
	for _, ch := range s2.Channels {
		sum := 0
		for _, mm := range ch.Msgs {
			sum += mm.Amount
		}
		got2[[2]int{ch.From, ch.To}] = sum
	}
	if got2[[2]int{2, 0}] != 33 {
		t.Fatalf("快照2在途应只含C(33), got %+v", got2)
	}
	if _, ok := got2[[2]int{1, 0}]; ok {
		t.Fatalf("快照2不应包含1->0在途: %+v", got2)
	}
}

// TestOutAfter：记录前发出、对端截止后到达或未到达的钱补记到发送方。
func TestOutAfter(t *testing.T) {
	e := newTestEnv()
	m := e.mgr
	s := m.getOrCreate(7)
	// 手工构造：0 在 t=10 记录，1、2 在 t=12 记录；所有入信道 t=20 关闭。
	taken := map[int]int64{0: 10, 1: 12, 2: 12}
	for node, at := range taken {
		r := m.nodeRec(s, node)
		r.done = true
		r.takenAt = at
		r.state = e.states[node]
		for a := 0; a < 3; a++ {
			if a != node {
				r.in[a].closed = true
				r.in[a].closedAt = 20
			}
		}
	}

	// 消息 t=2 由0发出（< 0记录时刻10），t=25 才到1（> 信道关闭20）=> outAfter+=40。
	late := dm(0, 1, 40, 3, 2)
	m.NoteSend(late)
	m.NoteDelivery(late, 25)
	if got := m.outAfter(s, 0); got != 40 {
		t.Fatalf("截止后到达 outAfter 应为40, got %d", got)
	}

	// 同类消息在截止前 t=15 到达 => 落入在途窗口，不补记。
	inWin := dm(0, 2, 9, 4, 2)
	m.NoteSend(inWin)
	m.NoteDelivery(inWin, 15)
	if got := m.outAfter(s, 0); got != 40 {
		t.Fatalf("窗口内消息不应补记, outAfter 应仍为40, got %d", got)
	}

	// 在对端记录(t=12)之前到达 => 已进余额，不补记。
	before := dm(0, 2, 5, 5, 2)
	m.NoteSend(before)
	m.NoteDelivery(before, 11)
	if got := m.outAfter(s, 0); got != 40 {
		t.Fatalf("对端记录前到达不应补记, got %d", got)
	}

	// 未到达（Build 时仍在网络）=> 也补记。
	notYet := dm(0, 1, 7, 6, 3)
	m.NoteSend(notYet)
	if got := m.outAfter(s, 0); got != 47 {
		t.Fatalf("未到达消息应补记, outAfter 应为47, got %d", got)
	}

	// t=10 之后才发出（切面之后）=> 不补记。
	after := dm(0, 1, 100, 8, 11)
	m.NoteSend(after)
	m.NoteDelivery(after, 30)
	if got := m.outAfter(s, 0); got != 47 {
		t.Fatalf("切面之后发出不应补记, got %d", got)
	}
}

// TestDuplicateInitiate 验证重复快照ID幂等。
func TestDuplicateInitiate(t *testing.T) {
	e := newTestEnv()
	m := e.mgr
	if !m.Initiate(1, 0, 0) {
		t.Fatal("首次发起应返回true")
	}
	if m.Initiate(1, 1, 5) {
		t.Fatal("重复ID应返回false且不重入")
	}
}
