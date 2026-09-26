package txn

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"idemresp/internal/clock"
)

func newTestDB(t *testing.T) (*DB, *clock.Fake) {
	t.Helper()
	dir := t.TempDir()
	clk := clock.NewFake(time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC))
	db, err := Open(dir, clk)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db, clk
}

func TestAcquireCommitReplay(t *testing.T) {
	db, _ := newTestDB(t)
	key := "k1"

	res, err := db.Acquire(key, "POST", "/v1/orders", "hash1", time.Minute)
	if err != nil || !res.Created {
		t.Fatalf("first acquire: created=%v err=%v", res.Created, err)
	}

	if err := db.Commit(key, 201, []byte(`{"ok":true}`),
		[]Order{{ID: "o1", Amount: 100, Currency: "USD", Status: "paid"}},
		[]LedgerEntryInput{{Kind: "charge", Detail: "ch_1", Amount: 100, OrderID: "o1"}}); err != nil {
		t.Fatalf("commit: %v", err)
	}

	// Same key + same hash: completed row is returned for replay.
	res2, err := db.Acquire(key, "POST", "/v1/orders", "hash1", time.Minute)
	if err != nil {
		t.Fatalf("replay acquire: %v", err)
	}
	if res2.Created {
		t.Fatal("replay must not create a new claim")
	}
	if res2.Existing.State != KeyCompleted || string(res2.Existing.ResponseBody) != `{"ok":true}` {
		t.Fatalf("unexpected replay row: %+v", res2.Existing)
	}
	if got := db.CountLedgerForKey(key); got != 1 {
		t.Fatalf("ledger count = %d, want 1", got)
	}
}

func TestConflictDifferentHash(t *testing.T) {
	db, _ := newTestDB(t)
	key := "k-conflict"

	if _, err := db.Acquire(key, "POST", "/v1/orders", "hashA", time.Minute); err != nil {
		t.Fatalf("acquire: %v", err)
	}
	if err := db.Commit(key, 201, []byte(`{}`), nil, nil); err != nil {
		t.Fatalf("commit: %v", err)
	}
	_, err := db.Acquire(key, "POST", "/v1/orders", "hashB", time.Minute)
	if err != ErrConflict {
		t.Fatalf("err = %v, want ErrConflict", err)
	}
}

func TestInProgressDuplicate(t *testing.T) {
	db, clk := newTestDB(t)
	key := "k-progress"
	lease := time.Minute

	if _, err := db.Acquire(key, "POST", "/x", "h", lease); err != nil {
		t.Fatalf("acquire: %v", err)
	}
	if _, err := db.Acquire(key, "POST", "/x", "h", lease); err != ErrInProgress {
		t.Fatalf("duplicate during processing: err=%v, want ErrInProgress", err)
	}

	// After the lease expires (simulated crash recovery), reclaim works.
	clk.Add(lease + time.Second)
	res, err := db.Acquire(key, "POST", "/x", "h", lease)
	if err != nil || !res.Created {
		t.Fatalf("post-lease acquire: created=%v err=%v", res.Created, err)
	}
	if res.Existing.Attempts != 2 {
		t.Fatalf("attempts = %d, want 2", res.Existing.Attempts)
	}
}

func TestFailAllowsImmediateRetry(t *testing.T) {
	db, _ := newTestDB(t)
	key := "k-fail"
	if _, err := db.Acquire(key, "POST", "/x", "h", time.Minute); err != nil {
		t.Fatalf("acquire: %v", err)
	}
	if err := db.Fail(key, errBoom{}); err != nil {
		t.Fatalf("fail: %v", err)
	}
	res, err := db.Acquire(key, "POST", "/x", "h", time.Minute)
	if err != nil || !res.Created {
		t.Fatalf("retry after fail: created=%v err=%v", res.Created, err)
	}
	if err := db.Commit(key, 201, []byte("{}"), nil,
		[]LedgerEntryInput{{Kind: "charge", Amount: 1}}); err != nil {
		t.Fatalf("commit: %v", err)
	}
	if got := db.CountLedgerForKey(key); got != 1 {
		t.Fatalf("ledger after retry = %d, want 1", got)
	}
}

type errBoom struct{}

func (errBoom) Error() string { return "boom" }

func TestCommitAtomicityInOneRecord(t *testing.T) {
	db, _ := newTestDB(t)
	key := "k-atomic"
	if _, err := db.Acquire(key, "POST", "/x", "h", time.Minute); err != nil {
		t.Fatalf("acquire: %v", err)
	}
	err := db.Commit(key, 201, []byte("{}"),
		[]Order{{ID: "o1", Amount: 5, Currency: "EUR", Status: "paid"}},
		[]LedgerEntryInput{
			{Kind: "charge", Amount: 5, OrderID: "o1"},
			{Kind: "coupon", Detail: "promo", Amount: -1, OrderID: "o1"},
		})
	if err != nil {
		t.Fatalf("commit: %v", err)
	}
	snap := db.Snapshot()
	if len(snap.Orders) != 1 || len(snap.Ledger) != 2 {
		t.Fatalf("snapshot = orders:%d ledger:%d, want 1/2", len(snap.Orders), len(snap.Ledger))
	}
	if snap.Keys[key].State != KeyCompleted {
		t.Fatalf("key state = %s", snap.Keys[key].State)
	}
}

func TestWALReplayAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	clk := clock.NewFake(time.Now().UTC())

	db, err := Open(dir, clk)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	key := "k-restart"
	if _, err := db.Acquire(key, "POST", "/x", "h", time.Minute); err != nil {
		t.Fatalf("acquire: %v", err)
	}
	if err := db.Commit(key, 201, []byte(`{"saved":1}`),
		[]Order{{ID: "o9", Amount: 42, Currency: "USD", Status: "paid"}},
		[]LedgerEntryInput{{Kind: "charge", Amount: 42, OrderID: "o9"}}); err != nil {
		t.Fatalf("commit: %v", err)
	}
	if err := db.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	db2, err := Open(dir, clk)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer func() { _ = db2.Close() }()

	row := db2.Snapshot().Keys[key]
	if row.State != KeyCompleted || string(row.ResponseBody) != `{"saved":1}` {
		t.Fatalf("key not recovered: %+v", row)
	}
	if got := db2.CountLedgerForKey(key); got != 1 {
		t.Fatalf("ledger recovered = %d, want 1", got)
	}
	if _, err := db2.Acquire(key, "POST", "/x", "h", time.Minute); err != nil {
		t.Fatalf("completed key should still replay after restart: %v", err)
	}
}

func TestWALTornTailRecovery(t *testing.T) {
	dir := t.TempDir()
	clk := clock.NewFake(time.Now().UTC())
	db, err := Open(dir, clk)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	goodKey := "k-good"
	if _, err := db.Acquire(goodKey, "POST", "/x", "h", time.Minute); err != nil {
		t.Fatalf("acquire: %v", err)
	}
	if err := db.Commit(goodKey, 201, []byte("{}"), nil,
		[]LedgerEntryInput{{Kind: "charge", Amount: 1}}); err != nil {
		t.Fatalf("commit: %v", err)
	}
	walPath := filepath.Join(dir, walName)
	info, err := os.Stat(walPath)
	if err != nil {
		t.Fatalf("stat wal: %v", err)
	}
	fullSize := info.Size()
	if err := db.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	// Simulate a torn write: append garbage beyond the good frames.
	f, err := os.OpenFile(walPath, os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		t.Fatalf("open wal append: %v", err)
	}
	if _, err := f.Write([]byte("CX\x00\x00\x00\xffgarbage-torn-tail")); err != nil {
		t.Fatalf("append garbage: %v", err)
	}
	_ = f.Close()

	db2, err := Open(dir, clk)
	if err != nil {
		t.Fatalf("reopen with torn tail: %v", err)
	}
	defer func() { _ = db2.Close() }()

	if got := db2.CountLedgerForKey(goodKey); got != 1 {
		t.Fatalf("good commit lost after torn-tail repair: %d", got)
	}
	// The torn bytes must have been truncated away.
	info2, _ := os.Stat(walPath)
	if info2.Size() != fullSize {
		t.Fatalf("wal size = %d, want repaired size %d", info2.Size(), fullSize)
	}
}

func TestConcurrentAcquiresSingleCommit(t *testing.T) {
	db, _ := newTestDB(t)
	key := "k-race"
	const n = 32

	start := make(chan struct{})
	created := make(chan bool, n)
	for i := 0; i < n; i++ {
		go func() {
			<-start
			res, err := db.Acquire(key, "POST", "/x", "h", time.Minute)
			if err == nil && res.Created {
				created <- true
				return
			}
			created <- false
		}()
	}
	close(start)

	wins := 0
	for i := 0; i < n; i++ {
		if <-created {
			wins++
		}
	}
	if wins != 1 {
		t.Fatalf("created claims = %d, want exactly 1", wins)
	}

	if err := db.Commit(key, 201, []byte("{}"), nil,
		[]LedgerEntryInput{{Kind: "charge", Amount: 1}}); err != nil {
		t.Fatalf("commit: %v", err)
	}
	if got := db.CountLedgerForKey(key); got != 1 {
		t.Fatalf("ledger = %d, want 1", got)
	}
}
