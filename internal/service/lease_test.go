package service

import (
	"context"
	"errors"
	"net/http/httptest"
	"testing"
	"time"

	"idempotentsave/internal/auditclient"
	"idempotentsave/internal/clock"
	"idempotentsave/internal/fakeaudit"
	"idempotentsave/internal/store"
)

// newShortTTLSvc 构造占位 TTL 很短的业务服务，用于验证代次回收与僵尸提交。
func newShortTTLSvc(t *testing.T, ttl time.Duration) (*Service, *clock.Fake, *store.Store) {
	t.Helper()
	clk := clock.NewFake()
	st, err := store.Open(store.Options{Dir: t.TempDir(), PendingTTL: ttl, Now: clk.Now})
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })

	audit := fakeaudit.New()
	ts := httptest.NewServer(audit.Handler())
	t.Cleanup(ts.Close)
	return New(st, auditclient.New(ts.URL), clk), clk, st
}

// 用短 TTL 存储验证代次 CAS：A 占位后过期，B 以新代次接管并提交，
// A 的迟到提交必须失败，且不得产生第二笔副作用。
func TestCommitLeaseLostViaShortTTL(t *testing.T) {
	svc, clk, st := newShortTTLSvc(t, 200*time.Millisecond)
	body := []byte(`{"account":"a","amount":10}`)
	d := dep("a", 10)

	leaseA, _, err := svc.Begin("k", d, body)
	if err != nil {
		t.Fatalf("begin A: %v", err)
	}
	clk.Advance(300 * time.Millisecond) // 超过 200ms TTL

	leaseB, _, err := svc.Begin("k", d, body)
	if err != nil {
		t.Fatalf("reclaim B: %v", err)
	}
	if leaseB.Gen != leaseA.Gen+1 {
		t.Fatalf("gen B=%d want %d", leaseB.Gen, leaseA.Gen+1)
	}
	// A 在 B 仍持占位时僵尸提交：代次 CAS 拒绝（ErrLeaseLost），副作用不增加
	if _, _, err := svc.Commit(leaseA, d, 1); !errors.Is(err, store.ErrLeaseLost) {
		t.Fatalf("stale commit err=%v, want lease lost", err)
	}
	if len(st.Ledger()) != 0 {
		t.Fatalf("stale commit must not add effects: ledger=%d", len(st.Ledger()))
	}
	// B 正常提交：恰好一笔
	if _, _, err := svc.Commit(leaseB, d, 1); err != nil {
		t.Fatalf("commit B: %v", err)
	}
	if len(st.Ledger()) != 1 {
		t.Fatalf("ledger=%d want 1", len(st.Ledger()))
	}
	// 此后 A 再提交会得到"已完成"（同样是拒绝，绝不可能重复入账）
	if _, _, err := svc.Commit(leaseA, d, 1); err == nil {
		t.Fatal("second stale commit must still be rejected")
	}
	// 旧租约的 release 也不能破坏 B 的已提交状态
	if err := svc.FailAndRelease(leaseA); err != nil {
		t.Fatalf("stale release: %v", err)
	}
	if rec, ok := svc.Lookup("k"); !ok || rec.Status != "completed" {
		t.Fatalf("B's committed record must survive stale release: %+v", rec)
	}
}

func TestFailAndReleaseIsIdempotent(t *testing.T) {
	svc, _, st := newSvc(t)
	body := []byte(`{"account":"a","amount":10}`)
	lease, _, err := svc.Begin("k", dep("a", 10), body)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	if err := svc.FailAndRelease(lease); err != nil {
		t.Fatalf("release: %v", err)
	}
	if err := svc.FailAndRelease(lease); err != nil {
		t.Fatalf("double release must be a harmless no-op: %v", err)
	}
	if len(st.Ledger()) != 0 {
		t.Fatal("released attempt must not have effects")
	}
	// 释放后等待点不存在：WaitForCompletion 返回 false 而非挂起
	if svc.WaitForCompletion(context.Background(), "k", 100*time.Millisecond) {
		t.Fatal("wait on released/absent key must be false")
	}
	if svc.CurrentHold("k") {
		t.Fatal("hold must be dropped after release")
	}
}
