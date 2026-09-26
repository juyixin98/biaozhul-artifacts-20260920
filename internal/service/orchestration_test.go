package service

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestDepositRequestValidate(t *testing.T) {
	cases := []struct {
		name string
		in   DepositRequest
		ok   bool
	}{
		{"valid", DepositRequest{Account: "a", Amount: 1}, true},
		{"missing account", DepositRequest{Amount: 1}, false},
		{"zero amount", DepositRequest{Account: "a", Amount: 0}, false},
		{"negative amount", DepositRequest{Account: "a", Amount: -1}, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := c.in.Validate()
			if c.ok && err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if !c.ok && err == nil {
				t.Fatal("expected validation error")
			}
		})
	}
}

func TestBeginConflictOnDifferentBody(t *testing.T) {
	svc, _, _ := newSvc(t)
	body1 := []byte(`{"account":"a","amount":10}`)
	lease, existing, err := svc.Begin("k", dep("a", 10), body1)
	if err != nil || lease == nil || existing != nil {
		t.Fatalf("begin1: lease=%v existing=%v err=%v", lease, existing, err)
	}
	if err := svc.FailAndRelease(lease); err != nil {
		t.Fatalf("release: %v", err)
	}
	// 释放后以不同正文占用同一键
	body2 := []byte(`{"account":"a","amount":20}`)
	lease2, _, err := svc.Begin("k", dep("a", 20), body2)
	if err != nil || lease2 == nil {
		t.Fatalf("begin2: %v", err)
	}
	if _, _, err := svc.Commit(lease2, dep("a", 20), 1); err != nil {
		t.Fatalf("commit2: %v", err)
	}
	// 再用第一种正文：冲突
	_, _, err = svc.Begin("k", dep("a", 10), body1)
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("want ErrConflict, got %v", err)
	}
}

func TestBeginInProgressReturnsSentinel(t *testing.T) {
	svc, _, _ := newSvc(t)
	body := []byte(`{"account":"a","amount":10}`)
	if _, _, err := svc.Begin("k", dep("a", 10), body); err != nil {
		t.Fatalf("begin: %v", err)
	}
	_, _, err := svc.Begin("k", dep("a", 10), body)
	if !errors.Is(err, ErrInProgress) {
		t.Fatalf("want ErrInProgress, got %v", err)
	}
	if !svc.CurrentHold("k") {
		t.Fatal("hold should exist while pending")
	}
}

func TestWaitForCompletionReturnsTrueAfterCommit(t *testing.T) {
	svc, audit, st := newSvc(t)
	_ = audit
	body := []byte(`{"account":"a","amount":10}`)
	d := dep("a", 10)
	lease, _, err := svc.Begin("k", d, body)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}

	done := make(chan bool, 1)
	go func() {
		done <- svc.WaitForCompletion(context.Background(), "k", 3*time.Second)
	}()

	time.Sleep(100 * time.Millisecond)
	if _, err := svc.RunExternal(context.Background(), lease, d); err != nil {
		t.Fatalf("external: %v", err)
	}
	if _, _, err := svc.Commit(lease, d, 1); err != nil {
		t.Fatalf("commit: %v", err)
	}

	select {
	case committed := <-done:
		if !committed {
			t.Fatal("wait should report committed=true")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("WaitForCompletion did not return after commit")
	}

	rec, ok := svc.Lookup("k")
	if !ok || rec.Status != "completed" {
		t.Fatalf("lookup after commit: ok=%v rec=%+v", ok, rec)
	}
	if len(st.Ledger()) != 1 {
		t.Fatal("effect missing")
	}
	if svc.CurrentHold("k") {
		t.Fatal("hold must be dropped after commit")
	}
}

func TestWaitForCompletionTimesOutWhilePending(t *testing.T) {
	svc, _, _ := newSvc(t)
	body := []byte(`{"account":"a","amount":10}`)
	if _, _, err := svc.Begin("k", dep("a", 10), body); err != nil {
		t.Fatalf("begin: %v", err)
	}
	start := time.Now()
	committed := svc.WaitForCompletion(context.Background(), "k", 200*time.Millisecond)
	if committed {
		t.Fatal("must report not-committed while still pending")
	}
	if time.Since(start) < 150*time.Millisecond {
		t.Fatal("wait returned before the timeout elapsed")
	}
}

func TestHoldResolveIsIdempotent(t *testing.T) {
	h := newHold()
	if h.State() != holdOpen {
		t.Fatal("new hold must be open")
	}
	h.doneCommitted()
	h.doneReleased() // 第二次 resolve 不得覆盖结局或 panic（重复 close 保护）
	if h.State() != holdCommitted {
		t.Fatalf("state=%d, first resolution must win", h.State())
	}
	select {
	case <-h.doneChan():
	default:
		t.Fatal("done channel must be closed after resolve")
	}
}

func TestLookupUnknownKey(t *testing.T) {
	svc, _, _ := newSvc(t)
	if _, ok := svc.Lookup("missing"); ok {
		t.Fatal("unknown key must report ok=false")
	}
}
