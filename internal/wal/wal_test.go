package wal

import (
	"path/filepath"
	"testing"
)

func TestAppendAndReplay(t *testing.T) {
	dir := t.TempDir()
	lg, err := Open(dir, "p1")
	if err != nil {
		t.Fatal(err)
	}
	in := []Record{
		{Kind: "prepared", TxnID: "T1", Writes: []KVWrite{{Key: "k", Value: "v"}}},
		{Kind: "commit", TxnID: "T1", Writes: []KVWrite{{Key: "k", Value: "v"}}},
	}
	for _, r := range in {
		if err := lg.Append(r); err != nil {
			t.Fatal(err)
		}
	}
	if err := lg.Close(); err != nil {
		t.Fatal(err)
	}

	out, err := ReadAll(dir, "p1")
	if err != nil {
		t.Fatal(err)
	}
	if len(out) != 2 || out[0].Kind != "prepared" || out[1].TxnID != "T1" {
		t.Fatalf("replay mismatch: %+v", out)
	}
	if out[0].Writes[0].Value != "v" {
		t.Fatalf("write payload lost: %+v", out[0])
	}
}

// A log that was never opened reads back empty (fresh node).
func TestReadMissingLog(t *testing.T) {
	recs, err := ReadAll(t.TempDir(), "ghost")
	if err != nil {
		t.Fatal(err)
	}
	if recs != nil {
		t.Fatalf("want nil, got %+v", recs)
	}
}

// Reopening appends after the existing tail rather than truncating it; this
// is what a restarted node relies on.
func TestReopenAppends(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir)
	lg, _ := Open(path, "c")
	_ = lg.Append(Record{Kind: "begin", TxnID: "T1"})
	_ = lg.Close()

	lg, _ = Open(path, "c")
	_ = lg.Append(Record{Kind: "commit", TxnID: "T1"})
	_ = lg.Close()

	recs, _ := ReadAll(path, "c")
	if len(recs) != 2 || recs[0].Kind != "begin" || recs[1].Kind != "commit" {
		t.Fatalf("reopen append corrupted log: %+v", recs)
	}
}
