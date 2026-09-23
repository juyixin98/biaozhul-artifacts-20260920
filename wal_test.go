package tpc

import (
	"path/filepath"
	"testing"
)

func TestWALAppendReplay(t *testing.T) {
	path := filepath.Join(t.TempDir(), "node.wal")

	w, recs, err := OpenWAL(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) != 0 {
		t.Fatalf("fresh WAL should be empty, got %d records", len(recs))
	}
	w.Append(Record{TxID: "t1", State: StatePreparing, Participants: []string{"http://a", "http://b"}})
	w.Append(Record{TxID: "t1", State: StateCommit})
	w.Append(Record{TxID: "t1", State: StateDone, Decision: StateCommit})
	w.Close()

	w2, recs2, err := OpenWAL(path)
	if err != nil {
		t.Fatal(err)
	}
	defer w2.Close()
	if len(recs2) != 3 {
		t.Fatalf("want 3 records, got %d", len(recs2))
	}
	if recs2[0].State != StatePreparing || len(recs2[0].Participants) != 2 {
		t.Fatalf("bad first record: %+v", recs2[0])
	}
	if recs2[2].Decision != StateCommit {
		t.Fatalf("bad DONE record: %+v", recs2[2])
	}
}
