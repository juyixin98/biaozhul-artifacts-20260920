package store

import (
	"errors"
	"fmt"
	"testing"
	"time"
)

func newTestStore(t *testing.T, ttl time.Duration) (*Store, *fakeNow) {
	t.Helper()
	dir := t.TempDir()
	now := &fakeNow{t: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)}
	st, err := Open(Options{Dir: dir, PendingTTL: ttl, Now: now.get})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st, now
}

type fakeNow struct{ t time.Time }

func (f *fakeNow) get() time.Time      { return f.t }
func (f *fakeNow) add(d time.Duration) { f.t = f.t.Add(d) }

func sampleEffect(account string, amount int64) *Effect {
	return &Effect{TxID: "tx1", Account: account, Amount: amount, Kind: "deposit"}
}

// commitWith 是测试辅助：用固定响应体走新的回调式 Commit 签名。
func commitWith(st *Store, gen uint64, key string, status int, body string, eff *Effect) (Record, error) {
	b := []byte(body)
	return st.Commit(gen, key, status, eff, func(_ int64) ([]byte, error) { return b, nil })
}

func TestCommitAndReplayIsIdempotent(t *testing.T) {
	st, now := newTestStore(t, time.Minute)

	lease, err := st.Begin("k", "fp", "dig")
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	if lease.Status != Pending {
		t.Fatalf("new lease must be pending, got %s", lease.Status)
	}
	if _, err := commitWith(st, lease.Gen, "k", 200, `{"ok":true}`, sampleEffect("a", 100)); err != nil {
		t.Fatalf("commit: %v", err)
	}

	// 再次 Begin 同摘要：直接拿回完成记录，不产生新占位
	rec, err := st.Begin("k", "fp", "dig")
	if err != nil {
		t.Fatalf("re-begin completed key: %v", err)
	}
	if rec.Status != Completed || rec.StatusCode != 200 {
		t.Fatalf("expected completed/200, got %s/%d", rec.Status, rec.StatusCode)
	}
	if got := st.Balance("a"); got != 100 {
		t.Fatalf("balance = %d, want 100", got)
	}
	if len(st.Ledger()) != 1 {
		t.Fatalf("ledger len = %d, want 1", len(st.Ledger()))
	}

	// 时间推进不改变已完成记录
	now.add(time.Hour)
	rec2, err := st.Begin("k", "fp", "dig")
	if err != nil || rec2.Status != Completed {
		t.Fatalf("completed record must survive time passing: %v %+v", err, rec2)
	}
}

func TestConflictOnDifferentFingerprint(t *testing.T) {
	st, _ := newTestStore(t, time.Minute)

	lease, err := st.Begin("k", "fp1", "d1")
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	if _, err := commitWith(st, lease.Gen, "k", 200, `{}`, sampleEffect("a", 10)); err != nil {
		t.Fatalf("commit: %v", err)
	}
	if _, err := st.Begin("k", "fp2", "d2"); !errors.Is(err, ErrConflict) {
		t.Fatalf("different fingerprint must conflict, got %v", err)
	}
	if len(st.Ledger()) != 1 {
		t.Fatal("conflict must not create effects")
	}
}

func TestInProgressWhileLeaseAlive(t *testing.T) {
	st, _ := newTestStore(t, time.Minute)
	if _, err := st.Begin("k", "fp", "d"); err != nil {
		t.Fatalf("begin: %v", err)
	}
	if _, err := st.Begin("k", "fp", "d"); !errors.Is(err, ErrInProgress) {
		t.Fatalf("concurrent same payload must be in-progress, got %v", err)
	}
}

func TestReleaseAllowsRetry(t *testing.T) {
	st, _ := newTestStore(t, time.Minute)
	lease, err := st.Begin("k", "fp", "d")
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	if err := st.Release(lease.Gen, "k"); err != nil {
		t.Fatalf("release: %v", err)
	}
	lease2, err := st.Begin("k", "fp", "d")
	if err != nil {
		t.Fatalf("retry after release should begin again: %v", err)
	}
	if lease2.Gen != lease.Gen+1 {
		t.Fatalf("retry gen = %d, want %d", lease2.Gen, lease.Gen+1)
	}
	if len(st.Ledger()) != 0 {
		t.Fatal("released attempt must not have effects")
	}
}

func TestTTLReclaimRejectsStaleCommit(t *testing.T) {
	st, now := newTestStore(t, 10*time.Second)

	// 尝试 A 占位后"死亡"（不提交）
	leaseA, err := st.Begin("k", "fp", "d")
	if err != nil {
		t.Fatalf("begin A: %v", err)
	}
	now.add(11 * time.Second) // 超过 TTL

	// 尝试 B 以新代次回收
	leaseB, err := st.Begin("k", "fp", "d")
	if err != nil {
		t.Fatalf("reclaim begin B: %v", err)
	}
	if leaseB.Gen != leaseA.Gen+1 {
		t.Fatalf("reclaimed gen = %d, want %d", leaseB.Gen, leaseA.Gen+1)
	}

	// A 僵尸复活试图提交：必须被拒
	if _, err := commitWith(st, leaseA.Gen, "k", 200, `zombie`, sampleEffect("a", 1)); !errors.Is(err, ErrLeaseLost) {
		t.Fatalf("stale commit must be rejected, got %v", err)
	}

	// B 正常提交：副作用恰好一次
	if _, err := commitWith(st, leaseB.Gen, "k", 200, `{"ok":true}`, sampleEffect("a", 100)); err != nil {
		t.Fatalf("commit B: %v", err)
	}
	if len(st.Ledger()) != 1 || st.Balance("a") != 100 {
		t.Fatalf("ledger=%v balance=%d, want one effect of 100", st.Ledger(), st.Balance("a"))
	}

	// A 的 release 不能误删 B 的状态
	if err := st.Release(leaseA.Gen, "k"); err != nil {
		t.Fatalf("stale release: %v", err)
	}
	if rec, ok := st.Lookup("k"); !ok || rec.Status != Completed {
		t.Fatalf("stale release must not remove B's record: %+v ok=%v", rec, ok)
	}
}

func TestCommitNilEffectReplaysFailure(t *testing.T) {
	st, _ := newTestStore(t, time.Minute)
	lease, err := st.Begin("k", "fp", "d")
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	rec, err := commitWith(st, lease.Gen, "k", 422, `{"error":"bad"}`, nil)
	if err != nil {
		t.Fatalf("commit nil effect: %v", err)
	}
	if rec.StatusCode != 422 || rec.Effect != nil {
		t.Fatalf("expected 422 with no effect, got %d %+v", rec.StatusCode, rec.Effect)
	}
	again, err := st.Begin("k", "fp", "d")
	if err != nil || again.Status != Completed || again.StatusCode != 422 {
		t.Fatalf("failure result must be replayed consistently: %v %+v", err, again)
	}
	if len(st.Ledger()) != 0 {
		t.Fatal("nil-effect commit must not touch ledger")
	}
}

func TestWALRecoveryReplaysCommitsAndPending(t *testing.T) {
	dir := t.TempDir()
	now := &fakeNow{t: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)}

	st, err := Open(Options{Dir: dir, PendingTTL: time.Minute, Now: now.get})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	lease, err := st.Begin("k1", "fp1", "d1")
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	if _, err := commitWith(st, lease.Gen, "k1", 200, `{"a":1}`, sampleEffect("acct", 250)); err != nil {
		t.Fatalf("commit: %v", err)
	}
	// 一个遗留 pending（模拟崩溃在提交前）
	if _, err := st.Begin("k2", "fp2", "d2"); err != nil {
		t.Fatalf("begin k2: %v", err)
	}
	if err := st.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	// 重开：等同进程崩溃后重放 WAL
	st2, err := Open(Options{Dir: dir, PendingTTL: time.Minute, Now: now.get})
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer func() { _ = st2.Close() }()

	if len(st2.Ledger()) != 1 || st2.Balance("acct") != 250 {
		t.Fatalf("commit not recovered: ledger=%v balance=%d", st2.Ledger(), st2.Balance("acct"))
	}
	r1, ok := st2.Lookup("k1")
	if !ok || r1.Status != Completed || string(r1.Response) != `{"a":1}` {
		t.Fatalf("completed record not recovered: ok=%v rec=%+v", ok, r1)
	}
	r2, ok := st2.Lookup("k2")
	if !ok || r2.Status != Pending {
		t.Fatalf("pending lease must survive crash: ok=%v rec=%+v", ok, r2)
	}
	// 遗留占位仍未过期（时间未推进），同摘要应判 in-progress
	if _, err := st2.Begin("k2", "fp2", "d2"); !errors.Is(err, ErrInProgress) {
		t.Fatalf("recovered pending must block same payload, got %v", err)
	}
}

func TestConcurrentBeginExactlyOneWinner(t *testing.T) {
	st, _ := newTestStore(t, time.Minute)
	const n = 100
	errs := make(chan error, n)
	winners := make(chan uint64, n)
	for i := 0; i < n; i++ {
		go func() {
			lease, err := st.Begin("conc", "fp", "d")
			if err == nil {
				winners <- lease.Gen
				return
			}
			errs <- err
		}()
	}
	winCount, inProgress := 0, 0
	for i := 0; i < n; i++ {
		select {
		case <-winners:
			winCount++
		case err := <-errs:
			if errors.Is(err, ErrInProgress) {
				inProgress++
			} else {
				t.Fatalf("unexpected begin error: %v", err)
			}
		}
	}
	if winCount != 1 || inProgress != n-1 {
		t.Fatalf("winners=%d inProgress=%d, want 1 and %d", winCount, inProgress, n-1)
	}
}

func TestResetClearsState(t *testing.T) {
	st, _ := newTestStore(t, time.Minute)
	lease, err := st.Begin("k", "fp", "d")
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	if _, err := commitWith(st, lease.Gen, "k", 200, `{}`, sampleEffect("a", 5)); err != nil {
		t.Fatalf("commit: %v", err)
	}
	if err := st.Reset(); err != nil {
		t.Fatalf("reset: %v", err)
	}
	if st.Stats().Ledger != 0 || st.Stats().Keys != 0 || st.Balance("a") != 0 {
		t.Fatalf("state not cleared: %+v", st.Stats())
	}
	if _, err := st.Begin("k", "fp", "d"); err != nil {
		t.Fatalf("begin after reset should be fresh: %v", err)
	}
}

func TestExpiresAtIsPersisted(t *testing.T) {
	dir := t.TempDir()
	now := &fakeNow{t: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)}
	st, err := Open(Options{Dir: dir, PendingTTL: 10 * time.Second, Now: now.get})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	if _, err := st.Begin("k", "fp", "d"); err != nil {
		t.Fatalf("begin: %v", err)
	}
	wantExpiry := now.t.Add(10 * time.Second)
	if err := st.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	st2, err := Open(Options{Dir: dir, PendingTTL: 10 * time.Second, Now: now.get})
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer func() { _ = st2.Close() }()
	rec, ok := st2.Lookup("k")
	if !ok {
		t.Fatal("record lost")
	}
	if !rec.ExpiresAt.Equal(wantExpiry) {
		t.Fatalf("expires_at = %v, want %v", rec.ExpiresAt, wantExpiry)
	}
}

// 编译期保证哨兵错误可被 fmt 包装后仍 errors.Is 命中。
func TestSentinelWrapping(t *testing.T) {
	wrapped := fmt.Errorf("ctx: %w", ErrConflict)
	if !errors.Is(wrapped, ErrConflict) {
		t.Fatal("wrapped ErrConflict must satisfy errors.Is")
	}
}
