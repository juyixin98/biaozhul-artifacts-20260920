package service

import (
	"context"
	"net/http/httptest"
	"testing"
	"time"

	"idempotentsave/internal/auditclient"
	"idempotentsave/internal/clock"
	"idempotentsave/internal/fakeaudit"
	"idempotentsave/internal/store"
)

func newSvc(t *testing.T) (*Service, *fakeaudit.Server, *store.Store) {
	t.Helper()
	clk := clock.NewFake()
	st, err := store.Open(store.Options{Dir: t.TempDir(), PendingTTL: time.Minute, Now: clk.Now})
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })

	audit := fakeaudit.New()
	ts := httptest.NewServer(audit.Handler())
	t.Cleanup(ts.Close)

	return New(st, auditclient.New(ts.URL), clk), audit, st
}

func dep(account string, amount int64) DepositRequest {
	return DepositRequest{Account: account, Amount: amount}
}

// runOnce 执行 Begin→外部调用→提交（或释放）的完整编排。
func runOnce(t *testing.T, svc *Service, key string, d DepositRequest, raw []byte) (int, bool) {
	t.Helper()
	ctx := context.Background()
	lease, existing, err := svc.Begin(key, d, raw)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	if existing != nil {
		return existing.StatusCode, true
	}
	calls, exErr := svc.RunExternal(ctx, lease, d)
	if exErr != nil {
		if err := svc.FailAndRelease(lease); err != nil {
			t.Fatalf("release: %v", err)
		}
		return 0, false
	}
	if _, _, err := svc.Commit(lease, d, calls); err != nil {
		t.Fatalf("commit: %v", err)
	}
	return 200, false
}

func TestDeterministicTxIDAcrossReplay(t *testing.T) {
	svc, _, st := newSvc(t)
	body := []byte(`{"account":"a","amount":10}`)
	runOnce(t, svc, "k", dep("a", 10), body)
	runOnce(t, svc, "k", dep("a", 10), body)

	led := st.Ledger()
	if len(led) != 1 {
		t.Fatalf("ledger=%d want 1", len(led))
	}
	if led[0].TxID == "" || led[0].TxID != txIDFor("k", 1) {
		t.Fatalf("tx_id not deterministic: %q want %q", led[0].TxID, txIDFor("k", 1))
	}
}

// 外部系统"已落账但响应丢失"边界：本地恰好一次，外部可能两次。
func TestExternalAmbiguousFailure_LocalOnceExternalMaybeTwice(t *testing.T) {
	svc, audit, st := newSvc(t)
	audit.RecordThenFailNextN(1) // 第一次：记录事件但返回失败
	body := []byte(`{"account":"a","amount":10}`)
	d := dep("a", 10)

	// 第一次：外部有事件但调用报错 → 占位释放，本地无提交
	lease1, _, err := svc.Begin("k", d, body)
	if err != nil {
		t.Fatalf("begin1: %v", err)
	}
	if _, exErr := svc.RunExternal(context.Background(), lease1, d); exErr == nil {
		t.Fatal("expected external call to fail")
	}
	if err := svc.FailAndRelease(lease1); err != nil {
		t.Fatalf("release: %v", err)
	}
	if len(st.Ledger()) != 0 {
		t.Fatal("failed attempt must not commit locally")
	}

	// 第二次：成功并提交
	if code, replay := runOnce(t, svc, "k", d, body); code != 200 || replay {
		t.Fatalf("retry code=%d replay=%v, want 200 fresh", code, replay)
	}

	// 本地副作用恰好一次
	if len(st.Ledger()) != 1 || st.Balance("a") != 10 {
		t.Fatalf("ledger=%v balance=%d, want one effect of 10", st.Ledger(), st.Balance("a"))
	}
	// 外部系统收到两条事件——明确不保证外部恰好一次
	if audit.EventCount() != 2 {
		t.Fatalf("external events=%d want 2 (exactly-once not provided across systems)", audit.EventCount())
	}
}

func TestValidationFailureCommitHasNoEffect(t *testing.T) {
	svc, _, st := newSvc(t)
	body := []byte(`{"account":"a","amount":-1}`)
	lease, existing, err := svc.Begin("k", dep("a", -1), body)
	if err != nil || existing != nil {
		t.Fatalf("begin: err=%v existing=%v", err, existing)
	}
	if _, err := svc.CommitValidationFailure(lease, "negative"); err != nil {
		t.Fatalf("commit validation failure: %v", err)
	}
	if len(st.Ledger()) != 0 {
		t.Fatal("validation failure must not create an effect")
	}
	// 重放
	lease2, existing2, err := svc.Begin("k", dep("a", -1), body)
	if err != nil {
		t.Fatalf("re-begin: %v", err)
	}
	if lease2 != nil || existing2 == nil || existing2.Status != store.Completed || existing2.StatusCode != 422 {
		t.Fatalf("expected replayed 422, lease=%v existing=%+v", lease2, existing2)
	}
}
