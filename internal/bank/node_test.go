package bank

import "testing"

func newTestNode(t *testing.T) (*Node, *[]int) {
	t.Helper()
	var sent []int // 记录发出金额
	n := New(0, 100,
		func(to, amount int, txn int64) { sent = append(sent, amount) },
		func(Event) {},
	)
	return n, &sent
}

// TestTransferDebitAndReceiveCredit：发起扣款、收款入账、总额守恒。
func TestTransferDebitAndReceiveCredit(t *testing.T) {
	a, _ := newTestNode(t)
	b, _ := newTestNode(t)
	b.id = 1

	if !a.Transfer(1, 30, 0) {
		t.Fatal("转账应成功")
	}
	if a.Balance() != 70 {
		t.Fatalf("发起方扣款后应为70, got %d", a.Balance())
	}
	b.Receive(0, 30, a.txnSeq, 1, false)
	if b.Balance() != 130 {
		t.Fatalf("收款方入账后应为130, got %d", b.Balance())
	}
	if a.Balance()+b.Balance() != 200 {
		t.Fatal("两端总额应守恒")
	}
}

// TestIdempotentDuplicate：同一 (from,txn) 重复投递只入账一次。
func TestIdempotentDuplicate(t *testing.T) {
	b, _ := newTestNode(t)
	b.id = 1
	for i := 0; i < 5; i++ {
		b.Receive(0, 30, 7, 1, i > 0)
	}
	if b.Balance() != 130 {
		t.Fatalf("重复投递5次应只入账一次, got %d", b.Balance())
	}
}

// TestDistinctSendersSameTxn：不同发送方使用相同事务号也必须分别入账。
func TestDistinctSendersSameTxn(t *testing.T) {
	b, _ := newTestNode(t)
	b.id = 2
	b.Receive(0, 10, 1, 1, false)
	b.Receive(1, 10, 1, 2, false) // 与上一条 txn 相同，但发送方不同
	if b.Balance() != 120 {
		t.Fatalf("不同发送方的同号事务应分别入账, got %d", b.Balance())
	}
}

// TestInsufficient：余额不足不扣款、不发消息。
func TestInsufficient(t *testing.T) {
	n, sent := newTestNode(t)
	if n.Transfer(1, 101, 0) {
		t.Fatal("超额转账应失败")
	}
	if n.Balance() != 100 || len(*sent) != 0 {
		t.Fatalf("失败时不应有副作用: bal=%d sent=%d", n.Balance(), len(*sent))
	}
}

// TestPositiveOnly：非正金额 panic。
func TestPositiveOnly(t *testing.T) {
	n, _ := newTestNode(t)
	defer func() {
		if recover() == nil {
			t.Fatal("非正金额应 panic")
		}
	}()
	n.Transfer(1, 0, 0)
}
