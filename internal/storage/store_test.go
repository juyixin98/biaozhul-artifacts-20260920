package storage

import (
	"context"
	"path/filepath"
	"testing"
	"time"
)

func newTestStore(t *testing.T, bank int) *Store {
	t.Helper()
	dir := t.TempDir()
	s, err := NewStore(context.Background(), Options{
		DSN:          "file:" + filepath.Join(dir, "test.db") + "?_txlock=immediate",
		BankSize:     bank,
		AllowedUnits: []byte{1},
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

func at(seq int) time.Time {
	return time.Date(2026, 9, 23, 10, 0, seq, 0, time.UTC)
}

func TestWriteThenRead(t *testing.T) {
	s := newTestStore(t, 100)
	ctx := context.Background()

	if _, err := s.Write(ctx, 1, 1, 10, []uint16{11, 22, 33}, "1.2.3.4:9", at(1), nil); err != nil {
		t.Fatal(err)
	}
	got, err := s.Read(ctx, 9, 5) // one untouched (0), three written, one untouched
	if err != nil {
		t.Fatal(err)
	}
	want := []uint16{0, 11, 22, 33, 0}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("index %d got %d want %d (full=%v)", i, got[i], want[i], got)
		}
	}
}

// Out-of-range writes must fail as a whole batch: nothing may be applied.
func TestWriteOutOfRangeFailsWholeBatch(t *testing.T) {
	s := newTestStore(t, 16) // addresses 0..15
	ctx := context.Background()

	// Batch that would touch address 16.
	if _, err := s.Write(ctx, 1, 1, 14, []uint16{9, 9, 9}, "c", at(1), nil); err != ErrOutOfRange {
		t.Fatalf("want ErrOutOfRange, got %v", err)
	}
	// Even the in-range prefix (14,15) must be untouched.
	got, err := s.Read(ctx, 14, 2)
	if err != nil {
		t.Fatal(err)
	}
	if got[0] != 0 || got[1] != 0 {
		t.Fatalf("registers partially written after out-of-range batch: %v", got)
	}

	// Start address itself past the bank.
	if _, err := s.Write(ctx, 2, 1, 16, []uint16{1}, "c", at(2), nil); err != ErrOutOfRange {
		t.Fatalf("want ErrOutOfRange, got %v", err)
	}
}

func TestReadOutOfRange(t *testing.T) {
	s := newTestStore(t, 8)
	if _, err := s.Read(context.Background(), 7, 2); err != ErrOutOfRange {
		t.Fatalf("want ErrOutOfRange got %v", err)
	}
}

// While a write is UPSERTed but uncommitted (midWrite hook), concurrent
// readers must NOT see any of the new values; after commit they see all.
func TestConcurrentReadersNeverSeeHalfBatch(t *testing.T) {
	s := newTestStore(t, 100)
	ctx := context.Background()

	// Seed old values 0..4.
	if _, err := s.Write(ctx, 1, 1, 0, []uint16{100, 100, 100, 100}, "seed", at(0), nil); err != nil {
		t.Fatal(err)
	}

	entered := make(chan struct{})
	release := make(chan struct{})
	done := make(chan error, 1)

	go func() {
		_, err := s.Write(ctx, 9, 1, 0, []uint16{1, 2, 3, 4}, "w", at(5), func() {
			close(entered)
			<-release
		})
		done <- err
	}()

	<-entered

	// Readers must block rather than see half the batch. A read that
	// completes here with partial/new data is the exact bug we forbid.
	readDone := make(chan []uint16, 1)
	go func() {
		v, err := s.Read(ctx, 0, 4)
		if err != nil {
			t.Errorf("concurrent read: %v", err)
		}
		readDone <- v
	}()

	select {
	case v := <-readDone:
		t.Fatalf("reader returned during uncommitted write with %v — half-batch visibility", v)
	case <-time.After(150 * time.Millisecond):
		// correct: readers wait for the committed snapshot
	}

	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	v := <-readDone
	want := []uint16{1, 2, 3, 4}
	for i := range want {
		if v[i] != want[i] {
			t.Fatalf("post-commit read %v want %v", v, want)
		}
	}
}

func TestReceiptHashChain(t *testing.T) {
	s := newTestStore(t, 100)
	ctx := context.Background()

	for seq := 1; seq <= 4; seq++ {
		r, err := s.Write(ctx, uint16(100+seq), 1, 0,
			[]uint16{uint16(seq), uint16(seq * 2)}, "x", at(seq), nil)
		if err != nil {
			t.Fatal(err)
		}
		if r.Receipt.TransactionID != uint16(100+seq) {
			t.Fatal("receipt transaction id mismatch")
		}
	}
	receipts, err := s.ListReceipts(ctx, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(receipts) != 4 {
		t.Fatalf("got %d receipts", len(receipts))
	}
	prev := ""
	for i, r := range receipts {
		if r.PrevHash != prev {
			t.Fatalf("receipt %d prev hash linkage broken", i)
		}
		if r.Hash != hashReceipt(prev, r) {
			t.Fatalf("receipt %d stored hash != recomputed", i)
		}
		if len(r.Hash) != 64 {
			t.Fatalf("receipt %d hash not 32-byte hex", i)
		}
		prev = r.Hash
	}

	broken, err := s.VerifyChain(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if broken != -1 {
		t.Fatalf("fresh chain reported broken at %d", broken)
	}
}

// Tampering with a receipt's payload must break verification. Two shapes:
// (a) flip a value while keeping blob length consistent -> hash mismatch on
// the edited row; (b) change the blob length -> scan error (corruption).
func TestVerifyDetectsTamperedReceipt(t *testing.T) {
	// (a) consistent-length tamper: seq 1's stored value changes but its hash
	// was computed over the original bytes -> chain breaks at seq 1.
	s := newTestStore(t, 100)
	ctx := context.Background()
	for seq := 1; seq <= 3; seq++ {
		if _, err := s.Write(ctx, uint16(seq), 1, 0,
			[]uint16{uint16(seq)}, "x", at(seq), nil); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := s.db.ExecContext(ctx,
		`UPDATE receipts SET value_blob = x'0002' WHERE seq = 1`); err != nil {
		t.Fatal(err)
	}
	broken, err := s.VerifyChain(ctx)
	if err != nil {
		t.Fatalf("consistent-length tamper should break hash, not scan: %v", err)
	}
	if broken != 1 {
		t.Fatalf("want broken seq 1, got %d", broken)
	}

	// (b) structural corruption: blob no longer matches 2*quantity.
	s2 := newTestStore(t, 100)
	if _, err := s2.Write(ctx, 1, 1, 0, []uint16{1}, "x", at(1), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := s2.db.ExecContext(ctx,
		`UPDATE receipts SET value_blob = x'FFFFFFFF' WHERE seq = 1`); err != nil {
		t.Fatal(err)
	}
	if _, err := s2.VerifyChain(ctx); err == nil {
		t.Fatal("blob/quantity corruption must surface as a verification error")
	}

	// (c) re-link attack: tamper seq 1 AND recompute hashes forward is out of
	// scope for an attacker without the canonicalizer, but simply overwriting
	// prev_hash of seq 2 is independently detected.
	s3 := newTestStore(t, 100)
	for seq := 1; seq <= 2; seq++ {
		if _, err := s3.Write(ctx, uint16(seq), 1, 0,
			[]uint16{uint16(seq)}, "x", at(seq), nil); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := s3.db.ExecContext(ctx,
		`UPDATE receipts SET prev_hash = lower(hex(randomblob(32))) WHERE seq = 2`); err != nil {
		t.Fatal(err)
	}
	broken, err = s3.VerifyChain(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if broken != 2 {
		t.Fatalf("want broken seq 2, got %d", broken)
	}
}

// Failed batches append no receipt.
func TestFailedBatchAppendsNoReceipt(t *testing.T) {
	s := newTestStore(t, 4) // 0..3
	ctx := context.Background()
	if _, err := s.Write(ctx, 1, 1, 3, []uint16{1, 2}, "x", at(1), nil); err != ErrOutOfRange {
		t.Fatalf("want ErrOutOfRange, got %v", err)
	}
	rs, err := s.ListReceipts(ctx, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(rs) != 0 {
		t.Fatalf("failed batch left %d receipts", len(rs))
	}
}

func TestUnitAllowList(t *testing.T) {
	s := newTestStore(t, 4)
	if !s.UnitAllowed(1) || s.UnitAllowed(2) {
		t.Fatal("unit allow list wrong")
	}
}
