package wal

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestAppendLoadRoundTrip(t *testing.T) {
	dir := t.TempDir()
	l, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	recs := []Record{
		{Seq: 1, Task: json.RawMessage(`{"id":"a","state":"PENDING"}`)},
		{Seq: 2, Task: json.RawMessage(`{"id":"a","state":"RUNNING"}`)},
		{Seq: 3, Task: json.RawMessage(`{"id":"b","state":"PENDING"}`)},
	}
	for _, r := range recs {
		if err := l.Append(r); err != nil {
			t.Fatal(err)
		}
	}
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}

	l2, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer l2.Close()
	snap, got, err := l2.Load()
	if err != nil {
		t.Fatal(err)
	}
	if snap != nil {
		t.Fatalf("unexpected snapshot")
	}
	if len(got) != 3 || got[2].Seq != 3 {
		t.Fatalf("loaded %+v", got)
	}
}

func TestCompactTruncatesAndReplays(t *testing.T) {
	dir := t.TempDir()
	l, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	for i := int64(1); i <= 5; i++ {
		if err := l.Append(Record{Seq: i, Task: json.RawMessage(`{"id":"a"}`)}); err != nil {
			t.Fatal(err)
		}
	}
	if err := l.Compact(Snapshot{Seq: 5, Tasks: []json.RawMessage{
		json.RawMessage(`{"id":"a","state":"COMPLETED"}`),
	}}); err != nil {
		t.Fatal(err)
	}
	// WAL must be empty after compaction.
	st, err := os.Stat(filepath.Join(dir, walName))
	if err != nil {
		t.Fatal(err)
	}
	if st.Size() != 0 {
		t.Fatalf("wal size after compact = %d, want 0", st.Size())
	}
	// New records keep appending fine and replay starts at seq 5.
	if err := l.Append(Record{Seq: 6, Task: json.RawMessage(`{"id":"b"}`)}); err != nil {
		t.Fatal(err)
	}
	snap, recs, err := l.Load()
	if err != nil {
		t.Fatal(err)
	}
	if snap == nil || snap.Seq != 5 || len(snap.Tasks) != 1 {
		t.Fatalf("snapshot = %+v", snap)
	}
	if len(recs) != 1 || recs[0].Seq != 6 {
		t.Fatalf("recs after compact = %+v", recs)
	}
}

func TestTornTailIsIgnored(t *testing.T) {
	dir := t.TempDir()
	l, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Append(Record{Seq: 1, Task: json.RawMessage(`{"id":"a"}`)}); err != nil {
		t.Fatal(err)
	}
	if err := l.Append(Record{Seq: 2, Task: json.RawMessage(`{"id":"b"}`)}); err != nil {
		t.Fatal(err)
	}
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}
	// Simulate a crash in the middle of writing the third record.
	p := filepath.Join(dir, walName)
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, append(b, []byte(`{"seq":3,"tas`)...), 0o644); err != nil {
		t.Fatal(err)
	}

	l2, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer l2.Close()
	_, recs, err := l2.Load()
	if err != nil {
		t.Fatalf("torn tail should be tolerated, got %v", err)
	}
	if len(recs) != 2 || recs[1].Seq != 2 {
		t.Fatalf("recovered recs = %+v", recs)
	}
	// The store must still be able to append through the open handle.
	if err := l2.Append(Record{Seq: 3, Task: json.RawMessage(`{"id":"c"}`)}); err != nil {
		t.Fatal(err)
	}
}
