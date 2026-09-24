package receipt

import (
	"database/sql"
	"path/filepath"
	"strings"
	"testing"
)

func openTestDB(t *testing.T) (*Store, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "receipts.db")
	key := []byte("unit-test-key")
	s, err := Open(path, key)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { s.Close() })
	return s, path
}

func appendN(t *testing.T, s *Store, n int) []Receipt {
	t.Helper()
	got := make([]Receipt, n)
	for i := 0; i < n; i++ {
		err := s.WithWriteTx(func(tx *sql.Tx) error {
			var e error
			got[i], e = s.RecordWrite(tx, uint16(100+i), 1, uint16(i*2),
				[]uint16{uint16(0xA000 + i), uint16(0xB000 + i)})
			return e
		})
		if err != nil {
			t.Fatalf("append %d: %v", i, err)
		}
	}
	return got
}

func TestChainLinksAndGenesis(t *testing.T) {
	s, path := openTestDB(t)
	rs := appendN(t, s, 3)

	if rs[0].PrevSHA != sha256Hex(GenesisSalt) {
		t.Fatal("first receipt must anchor on genesis hash")
	}
	for i := 1; i < 3; i++ {
		if rs[i].PrevSHA != rs[i-1].ChainSHA {
			t.Fatalf("receipt %d not linked to previous", i)
		}
		if rs[i].ChainSHA == rs[i-1].ChainSHA {
			t.Fatal("chain hash must differ across receipts")
		}
	}
	if !strings.HasPrefix(canonicalBody(rs[0].Seq, 100, 1, 0, 2, "a000b000"), "v1|") {
		t.Fatal("canonical body version prefix missing")
	}

	res, err := Verify(path, []byte("unit-test-key"))
	if err != nil {
		t.Fatal(err)
	}
	if !res.OK() || res.Count != 3 {
		t.Fatalf("verify: %+v", res)
	}
}

func TestVerifyWrongKey(t *testing.T) {
	s, path := openTestDB(t)
	appendN(t, s, 2)
	res, err := Verify(path, []byte("wrong-key"))
	if err != nil {
		t.Fatal(err)
	}
	if res.OK() {
		t.Fatal("HMAC must fail with the wrong key")
	}
	if res.FirstBroken != 1 || !strings.Contains(res.Problem, "HMAC") {
		t.Fatalf("unexpected failure: %+v", res)
	}
}

func TestVerifyTamperedRow(t *testing.T) {
	s, path := openTestDB(t)
	appendN(t, s, 3)

	// Tamper directly with the payload of receipt 2.
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`UPDATE receipts SET values_hex='ffff0000' WHERE seq=2`); err != nil {
		t.Fatal(err)
	}
	db.Close()

	res, err := Verify(path, []byte("unit-test-key"))
	if err != nil {
		t.Fatal(err)
	}
	if res.OK() || res.FirstBroken != 2 {
		t.Fatalf("tampering must be detected at seq 2: %+v", res)
	}
}

func TestVerifyDeletedRow(t *testing.T) {
	s, path := openTestDB(t)
	appendN(t, s, 3)

	db, _ := sql.Open("sqlite", path)
	if _, err := db.Exec(`DELETE FROM receipts WHERE seq=2`); err != nil {
		t.Fatal(err)
	}
	db.Close()

	res, err := Verify(path, []byte("unit-test-key"))
	if err != nil {
		t.Fatal(err)
	}
	if res.OK() {
		t.Fatal("deleted row must break the chain")
	}
}

func TestEmptyKeyRejected(t *testing.T) {
	if _, err := Open(filepath.Join(t.TempDir(), "x.db"), nil); err == nil {
		t.Fatal("empty key must be rejected")
	}
}

func TestExceptionAudit(t *testing.T) {
	s, _ := openTestDB(t)
	if err := s.RecordException(7, 1, 0x10, 0x02, "out of range"); err != nil {
		t.Fatal(err)
	}
	events, err := s.RecentEvents(10)
	if err != nil || len(events) != 1 || events[0].TransactionID != 7 || events[0].ExceptionCode != 2 {
		t.Fatalf("events=%v err=%v", events, err)
	}
}

func TestDuplicateTransactionIDsAllowed(t *testing.T) {
	// The receipt layer has no opinion on transaction ids: two writes with
	// the same txn must both be recorded (Modbus has no cross-request
	// dedupe). Server-level behavior is tested in the server package.
	s, path := openTestDB(t)
	for i := 0; i < 2; i++ {
		err := s.WithWriteTx(func(tx *sql.Tx) error {
			_, e := s.RecordWrite(tx, 42, 1, 0, []uint16{uint16(i + 1)})
			return e
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	var count int
	db, _ := sql.Open("sqlite", path)
	defer db.Close()
	db.QueryRow(`SELECT count(*) FROM receipts WHERE transaction_id=42`).Scan(&count)
	if count != 2 {
		t.Fatalf("want 2 receipts for txn 42, got %d", count)
	}
}
